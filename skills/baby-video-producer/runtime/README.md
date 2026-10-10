# 実行ガイド

目的はスキルが作成した入力を使い、費用と制作履歴を保持して動画を生成・編集すること。ゴールは`artifacts/final.mp4`と`inputs/description.md`を同じrunに保存することです。このコードは記事の判断・台本承認・内蔵画像生成をエージェントへ引き継ぎます。

## 目次

1. [準備](#準備)
2. [入力](#入力)
3. [実行と判断](#実行と判断)
4. [改訂と費用](#改訂と費用)
5. [既存動画](#既存動画)
6. [オフライン検証](#オフライン検証)

## 準備

すべてのコマンドを初期化後の作業ディレクトリから実行します。Python 3.11以上、Node.js 22以上、FFmpeg、ffprobeを用意してください。

```bash
npm ci --ignore-scripts
node node_modules/@remotion/cli/remotion-cli.js browser ensure
python3 scripts/setup-credentials.py
python3 -m baby_video doctor
```

`.env.local`へ利用者自身が`FAL_KEY`・`FISH_API_KEY`を入力します。ファイルの権限は600です。CLIはこのファイルだけを読み、継承されたAPIキーや別の設定ファイルへfallbackしません。`doctor`はキーの有無と実行環境を表示し、通信しません。

字幕同期にはDTWに対応するwhisper.cppと取得済みのsmallモデルを使います。モデル取得方法は[公式リポジトリ](https://github.com/ggml-org/whisper.cpp)を確認し、モデルファイルをjobの`whisper_model`へ指定してください。macOS以外ではNoto Sans JPなどの日本語フォントをOSへインストールしてください。フォントは配布に含みません。

## 入力

エージェントが`examples/job.live-template.json`を`work/<制作名>/job.json`へコピーし、必要な入力を同じフォルダへ作成します。job内の相対パスはjobファイルの親フォルダを基準に解決されます。

| フィールド | 内容 |
| --- | --- |
| article_url / article_file | 出典URLとsource.md。全文を確認し、使用事実・条件・出典を記録 |
| dialogue_candidates_file | `{"candidates": [{"text": "...", "estimated_seconds": 9, "evidence": "..."}, ...]}`。10案が必要 |
| selected_candidate / selection_reason | 採用案の0〜9の番号と短い理由 |
| voice_id / fish_model | 利用許可を確認した声とAPIモデル。声IDとモデル名を混同しない |
| fish_speed | 新規生成では1.1。Fishのprosody.speedへ送信 |
| image_prompt_file / video_prompt_file | 画像・映像のプロンプト。男の子の赤ちゃん、顔と口が見える構図 |
| reference_files | 使用許可のある参照画像。参照なしの架空顔はimage_reference_modeとnew_fictional_face_authorizedを使う |
| bgm_file | 利用可能なローカルBGMのパス。新規パイプラインでは必須 |
| whisper_model / whisper_cli / whisper_dtw_model | ローカルのsmallモデル、whisper-cli、small |
| budget_usd / cost_caps_usd | 今回許可された総額とaudio・video・lipsyncの各送信上限 |
| standing_spending_authorized | 今回の有料制作への許可を確認してからtrueにする。runの上限を共通台帳へ一度だけ加算 |
| review_policy | 初回台本承認後はscript_only。技術検査と判断は省略しない |
| timing_revision_authorized | 同じ根拠・主題・語調・予算の尺調整を自動改訂する許可 |

各確認フラグは実確認に基づいて設定します。モックの値を本番へ流用しません。`procedure_confirmed`はこの制作手順を確認した意味です。非公開資料は必要ありません。

```bash
python3 -m baby_video preview-job --job work/clip-v001/job.json
```

これは不足入力と**仮見積**を返し、APIを呼びません。コード中の料金は過去の参考値です。無料モデルを使う場合も、現在の料金と利用範囲をアカウントで確認し、正の送信上限を設定します。料金表示は[Fish](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech)、[H3](https://fal.ai/models/minimax/h3-max-turbo/image-to-video/api)、[Kling](https://fal.ai/models/fal-ai/kling-video/lipsync/audio-to-video/api)で確認してください。数値見積だけでは課金を許可しません。

## 実行と判断

```bash
python3 -m baby_video resume --job work/clip-v001/job.json --run runs/clip-v001
```

`resume`は1工程を実行・照会してエージェントへ戻します。`review_required`なら生成済みの実ファイルを確認し、run内へ証拠JSONを作って判断します。

```json
{
  "summary": "復号と実測尺を確認。実音声の聴取は未実施。",
  "checks": ["technical_checked"],
  "evidence_files": ["artifacts/voice.mp3"],
  "unverified": ["発音・イントネーションの聴取"]
}
```

```bash
python3 -m baby_video decide --run runs/clip-v001 --stage audio \
  --choice next --evidence runs/clip-v001/inputs/audio-review.json
```

初回dialogueだけ、利用者の実際の承認に対して`--actor human`を使います。他工程や自動尺調整は`codex_agent`です。台本承認後の内部引き継ぎを工程ごとのユーザー確認待ちにしません。知覚上の確認をしていない場合は`unverified`へ残し、確認済みと申告しません。checksはSKILL.mdの表に従います。

画像工程の`waiting`では、Codexが内蔵image_genを呼び、結果の画素を確認して取り込みます。

```bash
python3 -m baby_video import-image --run runs/clip-v001 \
  --image /absolute/path/generated.png --prompt work/clip-v001/image-prompt.txt \
  --tool-generated
```

CLIから内蔵画像ツールを呼び出すことはできません。字幕には実測時刻が必要です。必要なら手動のCLIで照合します。

```bash
python3 - <<'PY'
import json
from pathlib import Path
run = Path("runs/clip-v001")
selected = json.loads((run / "artifacts/selected-dialogue.json").read_text())
(run / "inputs/dialogue.txt").write_text(selected["text"])
PY
python3 -m baby_video align-captions --audio runs/clip-v001/artifacts/voice.mp3 \
  --text-file runs/clip-v001/inputs/dialogue.txt --model /absolute/path/ggml-small.bin \
  --dtw-model small --output runs/clip-v001/inputs/captions.json
```

照合に失敗した場合は`caption-alignment.json`を確認し、不確かな時刻で次へ進めません。

description工程では作業場所の`references/instagram-caption.md`を読み、完成台本とsource.mdから投稿本文を作成して`inputs/description.md`へ保存します。動画内字幕の`captions.json`とは別です。

## 改訂と費用

```bash
python3 -m baby_video status --run runs/clip-v001
python3 -m baby_video revise --job work/clip-v001/job.json --run runs/clip-v001 \
  --reason '尺調整: 同じ意味を保って言い回しを調整' --stage dialogue
```

尺調整では同runの初回人間承認、元記事と台本の保存履歴を維持します。音声は1.1倍で生成し、実測発話8.5〜10秒、音声全体が映像尺内という条件で採用します。速度変更・切断・無音追加で合わせません。無料の音声再生成に限り自動で行い、有料下流の再生成は追加依頼が必要です。

| 変更 | 主な影響 |
| --- | --- |
| 音声・生成速度 | audio、lipsync、edit、旧字幕時刻 |
| 台本 | dialogue、audio、lipsync、edit、description |
| 画像 | image、video、upscale、lipsync、edit |
| 字幕・BGM | edit |
| キャプション本文 | description |

予算・共有台帳は同runの改訂でリセットしません。通常の許可済み制作は`standing_spending_authorized: true`を使い、runごとの上限を共通台帳へ一度だけ登録します。互換用の旧モードにはUS$2のテスト上限がありますが、これは利用者の課金許可を意味しません。

falへの送信は保存request_idから照会します。受付不明、応答不明、プロセス中断では自動再送せず、予約額を保持します。

```bash
python3 -m baby_video reconcile-request --run runs/clip-v001 \
  --attempt 5 --request-id EXISTING_VERIFIED_REQUEST_ID
python3 -m baby_video confirm-failed --run runs/clip-v001 \
  --attempt 5 --actual-usd 0 --confirmed-terminal
python3 -m baby_video retry-failed --run runs/clip-v001 --stage video
python3 -m baby_video settle-cost --run runs/clip-v001 --attempt 5 --actual-usd 0.225
```

上のID・番号・費用は説明用です。サービスで結果と請求を確認した値だけを入力します。有料再試行には追加の実行許可が必要です。再試行の許可はCLIだけでは判定できず、エージェントが利用者の指示を確認します。

falの`public_expiring`メディア方針は期限付きCDN公開であり、非公開化ではありません。1時間の保持設定とJSON保存抑制を使います。個人画像を扱う場合は送信範囲を確認してください。署名URL・APIキー・生のプロバイダログを制作記録へ残しません。

## 既存動画

元の発話を変えず、字幕と任意のBGMだけを追加します。新規生成の8.5秒下限は適用しません。

```json
[{"text": "こんにちは", "from": 0.15, "to": 0.9}]
```

```bash
python3 scripts/edit-existing-video.py --video /absolute/path/original.mp4 \
  --captions /absolute/path/captions.json --bgm /absolute/path/licensed-music.mp3 \
  --output output/edited.mp4
```

BGMなしなら`--bgm`を省略します。元動画・既存出力は上書きしません。

## オフライン検証

```bash
python3 -m unittest discover -s tests -v
node --test scripts/fal-worker.test.mjs
npm run typecheck
python3 -m baby_video mock-integration --run runs/demo
```

模擬制作は無地の画像・映像とテスト信号を使います。Remotionの実書き出しを行いますが、Fish・falへの通信や課金はありません。モック判断は`mock_test`、知覚上の品質確認はfalseです。実アカウントでの本番生成や声質・口の同期は、この検証には含みません。
