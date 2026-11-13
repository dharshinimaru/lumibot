<!-- entry-source-sha256: 33fe66572f29213275a2589143d0cee366518b359bd073900f8f6a24e5c14a99 -->

# LumiBot AI Trading

[English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Français](README.fr.md) · **Deutsch** · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt.md) · [Русский](README.ru.md)

**Baue KI-Trading-Agenten. Teste sie mit historischen Daten. Handle mit derselben Strategie.**

LumiBot ist ein kostenloses Open-Source-Python-Framework für Trading-Bots. Schreibe Regeln in Python, lass KI-Agenten entscheiden oder kombiniere beides. Führe historische Backtests aus, prüfe Entscheidungen und Trades und verbinde einen Broker für simulierten oder echten Handel.

Diese Seite fasst die englische README und die Startseite der Dokumentation zusammen. Vollständige Beispiele stehen in der [englischen README](README.md) und der [Dokumentation](https://lumibot.lumiwealth.com/).

## Warum LumiBot

- **Natives KI-Trading:** Agenten recherchieren, bewerten und erteilen Orders anhand deiner Prompts.
- **Vor dem Handel backtesten:** Prüfe KI-Entscheidungen und Python-Regeln mit historischen Daten, Charts, Trades und Logs.
- **Dieselbe Strategie für Backtests und Handel:** Behalte die Strategieklasse; ändere Datenquelle und Broker-Startkonfiguration.
- **Mehrere Anlageklassen:** Aktien, Optionen, Futures, Devisen, Krypto und Prognosemärkte je nach [unterstütztem Broker](https://lumibot.lumiwealth.com/brokers.all.html).
- **Die Entscheidung erreicht einen Broker.** Prüfe Orders, Ausführungen und Entscheidungsprotokolle. [So funktioniert die Ausführung](https://lumibot.lumiwealth.com/execution_gap.html).

## Verbinde deinen KI-Assistenten

**Schneller entwickeln mit BotSpot MCP**

Nutze Claude, ChatGPT, Codex, Cursor, VS Code oder einen anderen MCP-kompatiblen Assistenten. Der Programmierassistent hilft beim Schreiben und Testen des Codes; die Agenten in der Strategie treffen ihre Entscheidungen während der Ausführung.

BotSpot MCP bietet Strategie-Skills, kostenlose Forschungsdaten, Backtests mit Charts, geplantes Cloud-Hosting, Community, Marktplatz und die Verknüpfung von Brokerkonten. Die Verbindung ist kostenlos; gehostete Nutzung hängt vom Tarif ab.

**[Verbinde deinen KI-Assistenten](https://botspot.trade/agents)**

Für lokale Programmierhilfe gibt es die [kostenlosen LumiBot-Trading-Skills](https://github.com/Lumiwealth/lumibot-trading-skills). Forschungswerkzeuge innerhalb einer Strategie erklärt der [BotSpot-MCP-Leitfaden](https://lumibot.lumiwealth.com/botspot_mcp.html).

## Drei Wege zur Strategie

1. **[KI-native Strategien](README.md#run-your-first-ai-backtest):** Bearbeite die Prompts des Trading-Agenten.
2. **[Python-Strategien](README.md#backtest-a-strategy):** Schreibe deine Regeln in Python.
3. **[Hybride Strategien](README.md#build-a-hybrid-strategy):** Kombiniere Python-Regeln und KI-Entscheidungen.

## Einstieg

Python 3.10 oder neuer ist erforderlich. Starte einen Beispiel-Backtest mit kostenlosen Tagesdaten:

```bash
pip install lumibot
lumibot demo
```

KI-Beispiele benötigen Modell-Zugangsdaten; Modellaufrufe können Kosten verursachen. Simulierter oder echter Handel benötigt ein Brokerkonto und dessen Zugangsdaten. Siehe [KI-Beispiel](README.md#run-your-first-ai-backtest), [CLI-Einrichtung](https://lumibot.lumiwealth.com/cli.html) und [Ausführungsmodi](https://lumibot.lumiwealth.com/strategy_run_modes.html). Backtests garantieren keine zukünftigen Ergebnisse.

## Superkräfte für Ihre Agenten — kostenlos

Verbinden Sie BotSpot MCP mit Ihrer LumiBot-Strategie: Wirtschaftsdaten, Nachrichten, SEC-Dokumente, Marketplace-Strategien und Wissen aus der Community.

![Superkräfte für Ihre Agenten — kostenlos](docs/assets/readme/botspot_mcp_superpowers.png)

Ist Ihr Assistent bereits mit BotSpot verbunden? Sagen Sie: „Verbinde dieses LumiBot-Projekt mit BotSpot MCP.“ Er kann den API-Schlüssel erstellen und die Einstellungen für Sie speichern.

**[Agenten verbinden](https://lumibot.lumiwealth.com/botspot_mcp.html)**

Führe diesen Befehl im Strategieordner aus, melde dich an und bestätige die Verbindung. Starte anschließend die Strategie erneut. Die Einstellungen werden in der `.env` des Projekts gespeichert; LumiBot fügt die Recherchewerkzeuge beim Erstellen des Agenten automatisch hinzu. Beschreibe im `system_prompt`, was er recherchieren soll, und verlange Quellen, Datumsangaben und Hinweise auf Lücken in historischen Daten.

```bash
lumibot connect botspot
```

[BotSpot MCP → LumiBot](README.md#strategy-botspot-research)

<!-- lumisafe:start -->
## LumiSafe: Einsteigermodus (Zusammenfassung)

LumiSafe ist ein optionaler Einsteigermodus im Paket `lumisafe/`, aufgebaut auf LumiBot. Er investiert einen einfachen Indexfonds-Plan, prüft jede Order mit festen Sicherheitsregeln (kein Hebel, keine Leerverkäufe, keine Optionen; Obergrenzen für Einzelaktien und Handelshäufigkeit; Bedenkzeit bei Panikverkäufen und beim Hinterherlaufen hinter Kursen) und zeigt dein Ergebnis immer neben „hättest du einfach den S&P 500 gehalten“. Er handelt **nur mit Papierkonten**.

Voraussetzungen: Python 3.10 oder neuer und ein kostenloses **Paper**-Konto bei Alpaca. Die vollständigen Befehle (höchstens zehn, vom Klonen bis zum ersten Papier-Trade) stehen in der [englischen LumiSafe-Kurzanleitung](README.md#lumisafe-quickstart); danach genügen `lumisafe init`, `lumisafe run-paper` und `lumisafe status`. `lumisafe next` zeigt, was du mit deinem eigenen Geld wann kaufen solltest; es platziert nie eine Order.

Lernsoftware. Keine Finanz-, Steuer- oder Rechtsberatung. Anlagen können an Wert verlieren.
<!-- lumisafe:end -->

## Framework erkunden

[Python-Beispiele](https://lumibot.lumiwealth.com/examples.html) · [KI-Beispiele](https://lumibot.lumiwealth.com/agents_examples.html) · [Strategy API](https://lumibot.lumiwealth.com/strategy_api_overview.html) · [Broker](https://lumibot.lumiwealth.com/brokers.all.html)

Beim Trading sind Verluste möglich. Eine historische Simulation garantiert keine Rendite.
