# pork-news-podcast

豚肉業界ニュース（Notion DB）を毎朝1本のポッドキャストに自動変換する個人用リポジトリ。

## 仕組み

```
毎日 JST 7:30  Claude クラウドルーティーン「豚肉業界ニュース日次収集」
               └─ ニュースを Notion DB に登録（既存・別リポジトリの話）

毎日 JST 8:30  Claude クラウドルーティーン「豚肉Podcast台本生成」
               ├─ Notion DB から未配信記事（Podcast配信済み=未チェック、収集日3日以内）を取得
               ├─ 8〜10分のナレーション台本を執筆
               ├─ episodes/YYYY-MM-DD/script.txt + meta.json をこのリポジトリに push
               └─ Notion の記事に「Podcast配信済み」チェック

push 検知      GitHub Actions（.github/workflows/build-episode.yml）
               ├─ Google Cloud Text-to-Speech で台本を音声化（scripts/build_episode.py）
               ├─ mp3 を GitHub Releases にアップロード（リポジトリ肥大化防止）
               ├─ docs/<token>/feed.xml（RSS）を更新して main にコミット
               └─ GitHub Pages が RSS を配信

iPhone         Apple Podcasts が RSS を定期チェックして新エピソードを自動ダウンロード
```

## フィード URL（Apple Podcasts に登録する URL）

```
https://diamondgrill.github.io/pork-news-podcast/5221e23a16c005c53eb924ce/feed.xml
```

Apple Podcasts アプリ → ライブラリ → 「…」→「URLで番組を追加」に上記を貼る。
公開リポジトリだが URL のトークン部分は推測不能で、RSS には `itunes:block` を入れているため
Podcast ディレクトリに掲載されることはない。

## セットアップ（初回のみ）

1. **Google Cloud で Text-to-Speech API キーを作る**
   1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクト作成（無料枠のみで完結、課金額は月0円想定）
   2. 「APIとサービス」→「ライブラリ」→ *Cloud Text-to-Speech API* を有効化
   3. 「APIとサービス」→「認証情報」→「認証情報を作成」→「APIキー」
   4. キーの制限で「Cloud Text-to-Speech API」のみに限定しておく（推奨）
2. **キーをこのリポジトリの Secret に登録**
   - Web: リポジトリ → Settings → Secrets and variables → Actions → New repository secret
   - 名前: `GOOGLE_TTS_API_KEY` / 値: 取得したキー
3. **Apple Podcasts に上記フィード URL を登録**

## カスタマイズ

- **声を変える**: `config.json` の `voices` 先頭を差し替える。候補一覧は
  `curl "https://texttospeech.googleapis.com/v1/voices?languageCode=ja-JP&key=APIキー"`
  で取得できる。先頭の声が使えない場合は自動で次の声に切り替わる。
- **番組名・説明**: `config.json` の `podcast_title` / `podcast_description`
- **長さ・構成・文体**: クラウドルーティーン側のプロンプトを編集
  （https://claude.ai/code/routines）
- **配信時刻**: ルーティーンの cron を変更（UTC 表記。JST 8:30 = `30 23 * * *`）

## トラブルシュート

- エピソードが届かない日 → まず [Actions](../../actions) の実行ログ、
  次に Notion のログ DB（「[豚肉Podcast]」プレフィックス）を確認
- 収集ルーティーンが動かなかった日は台本ルーティーンが「新着なし」でスキップする（正常動作）
- 音声だけ作り直したい → 該当日の `episodes/YYYY-MM-DD/episode.json` を削除して
  Actions の `build-episode` を手動実行（workflow_dispatch）
