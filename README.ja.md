<!-- entry-source-sha256: 15415ea6cd94d92e881bb8d161dc7c709dc7d7f7a4a917d0e51898c7a7c9d384 -->

# LumiBot AI Trading

[English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · **日本語** · [한국어](README.ko.md) · [Português](README.pt.md) · [Русский](README.ru.md)

**AI トレーディングエージェントを作り、バックテストし、同じ戦略で取引する。**

LumiBot は無料のオープンソース Python フレームワークです。Python のルール、AI エージェントの判断、または両方を組み合わせて取引ボットを作れます。過去データで検証し、判断と取引を確認して、模擬取引や実取引のブローカーに接続できます。

このページは英語 README とドキュメントのホームページの要約です。完全なコードは [英語 README](README.md) と [ドキュメント](https://lumibot.lumiwealth.com/) にあります。

## LumiBot を選ぶ理由

- **ネイティブな AI 取引：** プロンプトに基づき、エージェントが調査、推論、発注を行います。
- **取引前にバックテスト：** AI の判断と Python ルールを過去データで評価し、チャート、取引、ログを確認できます。
- **検証と取引で同じ戦略を使用：** 戦略クラスを維持し、データソースとブローカーの実行設定を変更します。
- **複数の資産クラス：** [対応ブローカー](https://lumibot.lumiwealth.com/brokers.all.html) に応じて、株式、オプション、先物、外国為替、暗号資産、予測市場に対応します。
- **判断をブローカーに届けます。** 注文、約定、判断の記録を確認できます。[実行の仕組み](https://lumibot.lumiwealth.com/execution_gap.html).

## AI アシスタントを接続

**BotSpot MCP で構築を効率化**

Claude、ChatGPT、Codex、Cursor、VS Code、または他の MCP 対応アシスタントを利用できます。コーディングアシスタントはコードの作成とテストを支援し、戦略内のエージェントは実行時の取引判断を行います。

BotSpot MCP では戦略構築スキル、無料の調査データ、チャート付きバックテスト、定期的なクラウド実行、コミュニティ、マーケットプレイス、ブローカー口座の接続を利用できます。接続は無料です。ホストされた機能の利用条件はプランによって異なります。

**[AI アシスタントを接続](https://botspot.trade/agents)**

ローカルでのコーディング支援には無料の [LumiBot トレーディングスキル](https://github.com/Lumiwealth/lumibot-trading-skills) を利用できます。戦略内の調査ツールは [BotSpot MCP ガイド](https://lumibot.lumiwealth.com/botspot_mcp.html) を参照してください。

## 戦略を作る 3 つの方法

1. **[AI ネイティブ戦略](README.md#run-your-first-ai-backtest)：** 取引エージェントのプロンプトを編集します。
2. **[Python 戦略](README.md#backtest-a-strategy)：** ルールを Python で記述します。
3. **[ハイブリッド戦略](README.md#build-a-hybrid-strategy)：** Python ルールと AI の判断を組み合わせます。

## はじめに

Python 3.10 以降が必要です。無料の日足データを使うサンプルのバックテストを実行できます。

```bash
pip install lumibot
lumibot demo
```

AI の例にはモデルの認証情報が必要で、呼び出しには料金がかかる場合があります。模擬取引や実取引にはブローカー口座と認証情報が必要です。[AI の例](README.md#run-your-first-ai-backtest)、[CLI 設定](https://lumibot.lumiwealth.com/cli.html)、[実行モード](https://lumibot.lumiwealth.com/strategy_run_modes.html) を参照してください。バックテストは将来の成績を保証しません。

## エージェントにスーパーパワーを。接続は無料

LumiBot戦略にBotSpot MCPを接続すると、経済データ、ニュース、SEC文書、Marketplaceの戦略、Communityの知識を利用できます。

![エージェントにスーパーパワーを。接続は無料](docs/assets/readme/botspot_mcp_superpowers.png)

すでにアシスタントをBotSpotに接続していますか？「このLumiBotプロジェクトをBotSpot MCPに接続して」と伝えてください。アシスタントがAPIキーを作成し、設定を保存できます。

**[エージェントを接続する](https://lumibot.lumiwealth.com/botspot_mcp.html)**

戦略のフォルダーで次のコマンドを実行し、サインインして接続を承認した後、戦略を再実行します。設定はプロジェクトの `.env` に保存され、エージェントを作成すると LumiBot が調査ツールを自動的に追加します。`system_prompt` に調査内容を記載し、出典、日付、過去データの制約を示すよう指示してください。

```bash
lumibot connect botspot
```

[BotSpot MCP → LumiBot](README.md#strategy-botspot-research)

<!-- lumisafe:start -->
## LumiSafe: 初心者モード(概要)

LumiSafe は LumiBot 上に構築された任意の初心者モードで、`lumisafe/` パッケージにあります。シンプルなインデックスファンドの計画で投資し、すべての注文を固定の安全ルール(レバレッジ・空売り・オプションは不可、個別株と取引頻度に上限、狼狽売りや高値追いには冷却期間)で確認し、結果を常に「S&P 500 をただ保有していた場合」と並べて表示します。取引は**ペーパー口座のみ**です。

必要なもの: Python 3.10 以上と、Alpaca の無料**ペーパー**口座。クローンから最初のペーパー取引までの全コマンド(10 個以内)は [英語版 LumiSafe クイックスタート](README.md#lumisafe-quickstart) にあります。その後は `lumisafe init`、`lumisafe run-paper`、`lumisafe status` を使います。

教育用ソフトウェアです。金融・税務・法律上の助言ではありません。投資は価値が下がることがあります。
<!-- lumisafe:end -->

## フレームワークを調べる

[Python の例](https://lumibot.lumiwealth.com/examples.html) · [AI の例](https://lumibot.lumiwealth.com/agents_examples.html) · [Strategy API](https://lumibot.lumiwealth.com/strategy_api_overview.html) · [ブローカー](https://lumibot.lumiwealth.com/brokers.all.html)

取引には損失のリスクがあります。過去のシミュレーションは利益を保証しません。
