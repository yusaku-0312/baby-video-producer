// Portrait captions and background audio with per-run props.
import React from 'react';
import {fitText} from '@remotion/layout-utils';
import {getVideoMetadata} from '@remotion/media-utils';
import {AbsoluteFill, Audio, Composition, interpolate, OffthreadVideo,
  Sequence, staticFile, useVideoConfig} from 'remotion';

type Caption = {text: string; from: number; to: number};
type Style = {fontFamily: string; fontSizeRatio: number; centerYRatio: number;
  fillColor: string; strokeColor: string; strokeWidthRatio: number};
export type VideoProps = {video: string; fps: number; bgm: string | null; captions: Caption[];
  originalVolume: number; bgmVolume: number; style: Style};

export const defaultProps: VideoProps = {
  video: 'source.mp4', fps: 30, bgm: null, captions: [], originalVolume: 1,
  bgmVolume: 0.12,
  style: {fontFamily: '"Hiragino Maru Gothic ProN", "Noto Sans JP", sans-serif',
    fontSizeRatio: 0.105, centerYRatio: 0.66, fillColor: '#ffffff',
    strokeColor: '#2b2b2b', strokeWidthRatio: 0.085},
};

const CaptionView: React.FC<{text: string; style: Style}> = ({text, style}) => {
  const {width, height} = useVideoConfig();
  const fitted = text.split('\n').map((line) => fitText({text: line,
    withinWidth: width * 0.88, fontFamily: style.fontFamily, fontWeight: 800}).fontSize);
  const fontSize = Math.min(width * style.fontSizeRatio, ...fitted);
  const shared: React.CSSProperties = {fontFamily: style.fontFamily,
    fontWeight: 800, fontSize, lineHeight: 1.3, whiteSpace: 'pre', textAlign: 'center'};
  return <AbsoluteFill>
    <div style={{position: 'absolute', top: height * style.centerYRatio,
      width: '100%', transform: 'translateY(-50%)', display: 'flex', justifyContent: 'center'}}>
      <div style={{position: 'relative', filter: 'drop-shadow(0 3px 4px #0005)'}}>
        <div style={{...shared, color: style.strokeColor,
          WebkitTextStroke: `${fontSize * style.strokeWidthRatio}px ${style.strokeColor}`}}>{text}</div>
        <div style={{...shared, color: style.fillColor, position: 'absolute', inset: 0}}>{text}</div>
      </div>
    </div>
  </AbsoluteFill>;
};

const Video: React.FC<VideoProps> = (props) => {
  const {fps, durationInFrames} = useVideoConfig();
  return <AbsoluteFill style={{backgroundColor: 'black'}}>
    <OffthreadVideo src={staticFile(props.video)} volume={props.originalVolume}/>
    {props.bgm ? <Audio src={staticFile(props.bgm)} loop volume={(frame) =>
      props.bgmVolume * interpolate(frame,
        [0, fps * 0.3, durationInFrames - fps, durationInFrames], [0, 1, 1, 0],
        {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'})}/> : null}
    {props.captions.map((caption, index) => {
      const from = Math.round(caption.from * fps);
      return <Sequence key={index} from={from}
        durationInFrames={Math.max(1, Math.round(caption.to * fps) - from)}>
        <CaptionView text={caption.text} style={props.style}/>
      </Sequence>;
    })}
  </AbsoluteFill>;
};

export const Root: React.FC = () => <Composition id="BabyVideo" component={Video}
  width={1080} height={1920} fps={30} durationInFrames={60} defaultProps={defaultProps}
  calculateMetadata={async ({props}) => {
    const metadata = await getVideoMetadata(staticFile(props.video));
    if (metadata.width !== 1080 || metadata.height !== 1920) {
      throw new Error('Input must pass the 1080x1920 portrait conversion first');
    }
    for (const caption of props.captions) {
      if (!caption.text || caption.from < 0 || caption.to <= caption.from ||
          caption.to > metadata.durationInSeconds + 0.05) {
        throw new Error('Caption timing is outside the input video');
      }
    }
    return {fps: props.fps, durationInFrames: Math.max(1,
      Math.round(metadata.durationInSeconds * props.fps))};
  }}/>;
