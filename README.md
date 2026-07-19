# 読み上げツール (Ctrl+Alt+クリック)

画面上に丸いアイコンで常駐し、**Ctrl+Alt を押しながら画面上の文章を左クリック**すると、
その位置から文末までを読み上げる Windows 用ツールです。

## 起動・再起動・終了

| 操作 | 方法 |
|---|---|
| 起動 | **`launch.vbs` をダブルクリック**(コンソール窓なしで常駐) |
| 終了 | 画面上の丸いアイコンを**右クリック → 終了** |
| 再起動 | 終了してから、もう一度 `launch.vbs` をダブルクリック |

起動中は画面に丸いアイコンが常に最前面で表示されます。
二重起動はできません(既に起動中の場合はメッセージが出ます)。

## 使い方

- **Ctrl+Alt + 左クリック** … クリックした位置から文末まで読み上げ
- **Esc** または **アイコンを左クリック** … 読み上げを停止
- **アイコンをドラッグ** … 好きな場所へ移動(位置は記憶されます)
- **アイコンを右クリック** … 停止 / 話速(6段階)/ 音声 / 終了

アイコンの色: 青 = 待機中、緑 = 読み上げ中、橙の点灯 = テキストを取得できなかった

設定は `config.json` に自動保存されます。

## 仕組み

- クリック検知: 低レベルマウスフック(Ctrl+Alt+クリックはアプリ側に渡らないよう抑制)
- テキスト取得: UI Automation の TextPattern でクリック位置〜文末を抽出
  (ブラウザ / メモ帳 / Word などで動作。テキストを公開しないアプリでは通知音+橙点灯)
- 読み上げ: Windows 標準の SAPI5(完全ローカル動作)

## セットアップ

```powershell
pip install -r requirements.txt
```

その後 `launch.vbs` をダブルクリック。

## 自然な音声について (導入済み)

[NaturalVoiceSAPIAdapter](https://github.com/gexgd0419/NaturalVoiceSAPIAdapter) v0.2.9
導入済み(`tools\NaturalVoiceSAPIAdapter\Installer.exe`)。音声メニューに出る日本語音声:

- **Microsoft Nanami (Natural)** … 完全ローカルの自然音声(既定・推奨)
- Microsoft Nanami / Keita **Online** (Natural) … Edge のオンライン音声(要ネット・テキストが Microsoft に送信される)
- Microsoft Haruka Desktop … Windows 標準(機械的だが最軽量)

### ローカル自然音声の仕組み(重要)

Windows のナレーター用自然音声 v1.0.5(インストール済み)は復号キー変更により
アダプタから使えない。そのため **v1.0.4 の音声モデルを展開して使っている**:

- モデル置き場: `tools\NarratorVoices\Nanami_1.0.4\`(アダプタ公式 Wiki 記載のリンクから取得)
- レジストリ: `HKCU\Software\NaturalVoiceSAPIAdapter\Enumerator` の
  `NarratorVoicePath` = `C:\Users\trbnp\text_to_speech\tools\NarratorVoices`
- このフォルダに `Tokens.xml` を含む音声フォルダを追加すると他の声も増やせる
  (男性声 Keita v1.0.4 も同じ Wiki ページから入手可能)

## Windows 起動時に自動起動する

PowerShell で一度実行すると、スタートアップに `launch.vbs` のショートカットが作られます:

```powershell
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\読み上げツール.lnk")
$s.TargetPath = "C:\Users\trbnp\text_to_speech\launch.vbs"
$s.WorkingDirectory = "C:\Users\trbnp\text_to_speech"
$s.Save()
```

## 制限事項

- テキストを UI Automation に公開しないアプリ(一部のゲーム、画像化された PDF 等)では
  読み上げできません。その場合は通知音が鳴り、アイコンが橙色に点灯します。
- クリック位置の特定に対応していないアプリでは、その画面の全文読み上げに
  フォールバックします。
