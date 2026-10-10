import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createFalClient} from '@fal-ai/client';
import {operate, onceOnlyFetch} from './fal-worker.mjs';

test('SDK retries cannot issue a second chargeable submission', async () => {
  let calls = 0;
  const aborter = new AbortController();
  const client = createFalClient({credentials: 'mock-key', fetch: onceOnlyFetch(async () => {
    calls += 1;
    throw new TypeError('fetch failed');
  }, aborter)});
  await assert.rejects(operate(client, {operation: 'submit',
    model: 'minimax/h3-max-turbo/image-to-video', input: {prompt: 'mock'},
    mediaPolicy: 'public_expiring', expirationSeconds: 3600, abortSignal: aborter.signal}));
  assert.equal(calls, 1);
});

test('COMPLETED with provider error is marked failed without exposing payload', async () => {
  const result = await operate({queue: {status: async () => ({status: 'COMPLETED',
    error: 'contains sensitive provider context'})}}, {operation: 'status',
    model: 'fal-ai/kling-video/lipsync/audio-to-video', request_id: 'mock-1'});
  assert.deepEqual(result, {request_id: 'mock-1', status: 'COMPLETED', failed: true});
});

test('payload storage and runner retries are disabled independently from input upload ACL', async () => {
  let options;
  const result = await operate({queue: {submit: async (_, input) => {
    options = input; return {request_id: 'mock-2'};
  }}}, {operation: 'submit', model: 'minimax/h3-max-turbo/image-to-video',
    input: {prompt: 'mock'}, mediaPolicy: 'public_expiring', expirationSeconds: 3600});
  assert.equal(result.request_id, 'mock-2');
  assert.equal(options.headers['X-Fal-Store-IO'], '0');
  assert.equal(options.headers['X-Fal-No-Retry'], '1');
  assert.equal(JSON.parse(options.headers['X-Fal-Object-Lifecycle-Preference']).expiration_duration_seconds, 3600);
});

test('unverified alternate model cannot be submitted', async () => {
  await assert.rejects(operate({}, {operation: 'submit', model: 'unverified-model'}));
});
