# 赤ちゃん動画制作スキル

育児記事の調査から、10秒以内の赤ちゃん縦動画とInstagram用キャプションを作るCodexスキルです。目的は、根拠を確認した育児情報を短い動画にすること。ゴールは、1080×1920のMP4と貼り付け用のキャプション本文を一緒に受け取ることです。

台本を確認した後は、音声・画像・動画・字幕・BGM・キャプションまで進めます。既存動画への字幕・BGM追加や、途中の制作の再開にも対応します。

## 目次

1. [できること](#できること)
2. [必要な環境](#必要な環境)
3. [インストール](#インストール)
4. [使い方](#使い方)
5. [設定と費用](#設定と費用)
6. [検証](#検証)
7. [ライセンス](#ライセンス)

## できること

```text
育児記事・一次情報 → 事実確認 → 台本10案・採用台本の承認
                                   ↓
                         Fish音声（生成速度1.1）
                                   ↓
架空の男の子の赤ちゃん画像 → H3動画 → 1080×1920変換
                                   ↓
                   Klingリップシンク ← 生成した音声
                                   ↓
                 実測音声から字幕 → Remotion・BGM
                                   ↓
                   完成MP4 ＋ 投稿キャプション全文
```

実行コードをスキル内に同梱しています。他のプロジェクトのファイルや、作者の制作履歴を必要としません。音声ID、BGM、投稿アカウント、保存先は利用者が設定します。初期設定では架空の男の子の赤ちゃんを使います。

## 必要な環境

| 用途 | 必要なもの |
| --- | --- |
| エージェント | ローカルファイル・シェル・Web検索・画像確認・内蔵image_genを使えるCodex環境 |
| ローカル処理 | Python 3.11以上、Node.js 22以上、npm、FFmpeg、ffprobe |
| 音声 | Fish AudioのAPIキーと、利用範囲に合う音声ID |
| 動画・リップシンク | fal.aiのAPIキーと生成料金の残高 |
| 字幕 | DTWに対応するwhisper.cppのwhisper-cliとローカルのsmallモデル |
| BGM・文字 | 利用許諾を確認したローカル音源、日本語フォント |

主な対象はmacOS / Linuxです。Windowsのネイティブ環境は未検証です。内蔵image_genがない環境では新規画像の工程を実行できません。CLIだけで記事の判断や画像生成まで自動化するものではありません。既存動画の編集とオフラインテストにはFish・falのキーは不要です。

## インストール

リポジトリを取得し、`skills/baby-video-producer` **全体**をスキル保存先へコピーします。`SKILL.md`だけをコピーすると実行コードが不足します。

```bash
git clone https://github.com/yusaku-0312/baby-video-producer.git
cd baby-video-producer
python3 - <<'PY'
from pathlib import Path
import shutil
destination = Path.home() / ".agents/skills/baby-video-producer"
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree("skills/baby-video-producer", destination)
print(destination)
PY
```

同名スキルがある場合は上書きせず停止します。Codexのユーザー用保存先は`~/.agents/skills`、プロジェクト用は`.agents/skills`です。[公式の配置案内](https://learn.chatgpt.com/docs/build-skills)を参照してください。既存の`~/.codex/skills`を使う環境では上記のコピー先を合わせてください。インストール後、Codexを再起動するかスキル一覧を更新します。

## 使い方

Codexへ次のように依頼します。

```text
$baby-video-producer を使って、育児記事を探すところから動画を1本作って。
作業場所は ./baby-video-production。台本を確認してから完成まで進めて。
```

記事URLやテーマを指定しても構いません。

```text
$baby-video-producer で、この育児記事から動画とキャプションを作って。
記事URL: https://example.com/article
```

既存動画の編集や再開では対象を指定します。

```text
$baby-video-producer で、./original.mp4 に短い字幕と手元のBGMを付けて。
```

```text
$baby-video-producer で、./baby-video-production/runs/clip-v001 を再開して。
jobは ./baby-video-production/work/clip-v001/job.json。
```

初回はスキルの初期化スクリプトが指定先へ実行コードをコピーします。その作業場所で依存関係を準備します。初期化だけではパッケージのインストールや生成APIへの通信は行いません。

```bash
python3 "$HOME/.agents/skills/baby-video-producer/scripts/init-workspace.py" \
  --workspace ./baby-video-production
cd baby-video-production
npm ci --ignore-scripts
node node_modules/@remotion/cli/remotion-cli.js browser ensure
python3 scripts/setup-credentials.py
python3 -m baby_video doctor
```

`setup-credentials.py`が作る`.env.local`に利用者自身がキーを入力します。キーをチャットに貼る必要はありません。whisper.cppの導入とモデル取得は[公式リポジトリ](https://github.com/ggml-org/whisper.cpp)を参照し、`whisper_model`へ取得済みモデルのパスを設定します。詳しいCLIと設定は[実行ガイド](skills/baby-video-producer/runtime/README.md)にあります。

## 設定と費用

本番設定のひな形は[examples/job.live-template.json](skills/baby-video-producer/runtime/examples/job.live-template.json)です。音声IDと予算は未設定、課金・権利・データ方針の確認フラグはfalseです。確認していない値をtrueに変更しないでください。新規制作はBGM音源の指定が必要です。既存動画編集ではBGMを省略できます。

Fish、H3、Klingの生成にはサービスごとの費用が発生し得ます。内蔵画像生成にも利用枠が必要です。料金は本番実行前に利用者のアカウントで確認し、総額と送信ごとの上限を設定します。スキルの取得だけでは課金は発生しません。作者の過去の課金承認を利用者へ引き継ぐこともありません。

台本承認後は工程ごとの確認を挟まず進めますが、有料素材の再生成、予算超過、投稿は元の制作許可に含めません。受付不明のAPI要求を自動再送せず、予約費用を残して照会します。音声・BGM・人物画像・記事の使用条件は利用者の制作範囲に合わせて確認します。

外部サービスの仕様は変わるため、本番実行時は[Fish TTS](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech)、[H3](https://fal.ai/models/minimax/h3-max-turbo/image-to-video/api)、[Kling](https://fal.ai/models/fal-ai/kling-video/lipsync/audio-to-video/api)も確認してください。

## 検証

同梱のテストはAPIをモックし、課金せずに再開、費用上限、二重送信防止、字幕、音声尺、台本承認の扱いを確認します。

```bash
cd skills/baby-video-producer/runtime
npm ci --ignore-scripts
python3 -m unittest discover -s tests -v
node --test scripts/fal-worker.test.mjs
npm run typecheck
```

Remotionを含む模擬制作の確認もできます。出力は無地の映像とテスト信号で、実際の赤ちゃん動画ではありません。

```bash
node node_modules/@remotion/cli/remotion-cli.js browser ensure
python3 -m baby_video mock-integration --run runs/demo
```

初回公開時の確認（2026年10月10日）：Pythonの制作テスト106件、配布テスト3件、fal SDKテスト4件、TypeScript型検査、スキル形式の検証が成功しました。インストールしたスキルだけを使い、空白を含む別の作業フォルダで模擬制作を完走。出力は1080×1920、9.003秒、H.264/yuv420p、AACで、復号を確認しました。生成APIへの通信・課金は行っていません。

利用者のアカウントでの本番生成、声質、口の同期は、この配布検証には含みません。

## ライセンス

このリポジトリのスキル文書と独自コードは[MIT License](LICENSE)です。APIサービス、音声、BGM、フォント、生成物の権利はそれぞれの条件に従います。Remotion自体の商用利用条件は[公式ライセンス](https://www.remotion.dev/docs/license)を確認してください。実制作の動画・画像・音源、第三者の投稿本文、APIキー、制作履歴は配布に含めていません。
