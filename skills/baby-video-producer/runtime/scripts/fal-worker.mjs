// JSON stdin/stdout bridge. Credentials stay in the inherited environment.
// Only safe fields are returned; never print raw responses or runner logs.
import {readFile} from 'node:fs/promises';
import {basename} from 'node:path';
import {fal} from '@fal-ai/client';

const allowedModels = new Set([
  'minimax/h3-max-turbo/image-to-video',
  'fal-ai/kling-video/lipsync/audio-to-video',
]);

export async function operate(client, request) {
  if (request.operation !== 'upload' && !allowedModels.has(request.model)) {
    throw new Error('Model is not in the verified allowlist');
  }
  if (request.operation === 'upload') {
    if (request.mediaPolicy !== 'public_expiring') throw new Error('Media policy missing');
    const data = await readFile(request.file);
    const file = new File([data], basename(request.file), {type: request.contentType});
    const url = await client.storage.upload(file, {lifecycle: {
      expiresIn: request.expirationSeconds, initialAcl: {default: 'allow', rules: []},
    }});
    return {url}; // Passed directly back to Python, never logged or persisted.
  }
  if (request.operation === 'submit') {
    if (request.mediaPolicy !== 'public_expiring') throw new Error('Media policy missing');
    const response = await client.queue.submit(request.model, {
      input: request.input, abortSignal: request.abortSignal,
      headers: {'X-Fal-Store-IO': '0', 'X-Fal-No-Retry': '1',
        'X-Fal-Object-Lifecycle-Preference': JSON.stringify({
          expiration_duration_seconds: request.expirationSeconds,
          initial_acl: {default: 'allow', rules: []},
        })},
    });
    return {request_id: response.request_id};
  }
  if (request.operation === 'status') {
    const response = await client.queue.status(request.model, {
      requestId: request.request_id, logs: false,
    });
    return {request_id: request.request_id, status: response.status,
      failed: Boolean(response.error || response.error_type)};
  }
  if (request.operation === 'result') {
    const response = await client.queue.result(request.model, {requestId: request.request_id});
    if (response.data?.error || response.data?.error_type || !response.data?.video?.url) {
      throw new Error('Provider returned no usable video');
    }
    return {request_id: request.request_id, url: response.data.video.url};
  }
  throw new Error('Unknown operation');
}

export function onceOnlyFetch(fetchImpl, aborter) {
  let submissionStarted = false;
  return async (url, init) => {
    const endpoint = new URL(typeof url === 'string' ? url : url.url ?? url.href);
    const submission = endpoint.hostname === 'queue.fal.run' && init?.method?.toUpperCase() === 'POST';
    if (submission && submissionStarted) throw new Error('Unsafe POST retry blocked');
    if (submission) submissionStarted = true;
    try {
      const response = await fetchImpl(url, init);
      if (submission && !response.ok) aborter.abort();
      return response;
    } catch {
      if (submission) aborter.abort();
      throw new Error('Transport unavailable');
    }
  };
}

async function main() {
  try {
    if (!process.env.FAL_KEY) throw new Error('Missing environment credential');
    let input = '';
    for await (const chunk of process.stdin) input += chunk;
    const request = JSON.parse(input);
    const aborter = new AbortController();
    fal.config({retry: {maxRetries: 0}, fetch: onceOnlyFetch(fetch, aborter)});
    request.abortSignal = aborter.signal;
    const result = await operate(fal, request);
    process.stdout.write(JSON.stringify(result));
  } catch {
    process.stdout.write(JSON.stringify({error: 'fal_operation_failed'}));
    process.exitCode = 2;
  }
}

if (process.argv[1]?.endsWith('fal-worker.mjs')) await main();
