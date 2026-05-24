# G2 Hello World (Python)

[English README](README.md)

このワークスペースには、[Mentra OSのEven G2実装](https://github.com/Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt)を参考にした Python 製の Even G2 BLE サンプルアプリを追加しています。

even_g2_hello.py では、Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt から流用・翻案したブロックを `BEGIN/END adapted from Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt` コメントで区切っています。

PCとEven G2を直接接続し、文字列を送信します。EvenアプリおよびMentra OSアプリ、スマートフォンに依存しません。

このアプリが行うこと:

- Even G2 の左右レンズを BLE スキャンして接続
- 通知を購読して左右の認証 ACK を待機
- Kotlin 実装の起動シーケンスに沿って初期化
- EvenHub ページを作成して Hello World を表示
- EvenHub Heart Beat と DevSettings Heart Beat を定期送信

![](image.png)

## 前提

- Windows で Bluetooth が有効になっていること
- Python 3.10 以降
- Even G2 の左右レンズが近くでアドバタイジングしていること (既存のアプリとの接続を完全に切断するか、スマートフォンの電源を切ってください。)

## インストール

```powershell
py -3 -m pip install -r requirements.txt
```

## 実行

近くに 1 台だけ G2 がある場合:

```powershell
py -3 even_g2_hello.py
```

表示文字列を位置引数で指定する場合:

```powershell
py -3 even_g2_hello.py "任意の文字列"
```

複数語は空白で連結されます:

```powershell
py -3 even_g2_hello.py Hello Even G2
```

複数台ある場合はシリアルで絞ってください:

```powershell
py -3 even_g2_hello.py --serial 2401 "Hello World"
```

従来どおり `--text` でも指定できます:

```powershell
py -3 even_g2_hello.py --text "任意の文字列"
```

## オプション

- `message`: 位置引数の表示文字列。複数語を渡した場合は空白で連結
- `--serial`: 対象シリアル番号の一部
- `--text`: 表示文字列の明示指定。位置引数の代替
- `--scan-timeout`: スキャン秒数。既定値は `8.0`
- `--heartbeat-seconds`: Heart Beat 送信間隔。既定値は `5.0`
- `--log-level`: `DEBUG` / `INFO` / `WARNING` / `ERROR`

## 注意

- 実機なしでは BLE 接続の疎通確認はできません。
- Even G2 のファームウェア差分によっては、追加の初期化コマンドが必要になる可能性があります。
- このサンプルはテキスト表示と Heart Beat 維持に絞っています。

# LICENSE

MIT LICENSE

[Mentra-Community/MentraOS : MIT LICENSE](https://github.com/Mentra-Community/MentraOS/blob/dev/LICENSE)
