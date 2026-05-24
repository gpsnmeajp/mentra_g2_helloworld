# G2.kt コードレポート

https://github.com/Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt

## 概要

| 項目 | 内容 |
|------|------|
| パッケージ | `com.mentra.bluetoothsdk.sgcs` |
| クラス名 | `G2` |
| 継承元 | `SGCManager` |
| 対象デバイス | Even Realities G2 スマートグラス |
| プラットフォーム | Android (Bluetooth LE / GATT) |

Even Realities G2 スマートグラスと Android 端末を BLE (Bluetooth Low Energy) で接続し、表示・音声・設定・メニュー等を制御するクライアント実装。

---

## ファイル構成（モジュール一覧）

```
G2.kt
├── G2BLE                    (BLE プロトコル定数)
├── ServiceID                (サービス ID 列挙)
├── EvenHubCmd               (EvenHub コマンド ID 列挙)
├── EvenHubResponseCmd       (EvenHub レスポンス ID 列挙)
├── OsEventType              (OS イベント種別列挙)
├── G2SettingCommandId       (G2 設定コマンド ID 列挙)
├── DevCfgCommandId          (デバイス設定コマンド ID 列挙)
├── calcCRC16()              (CRC16 計算関数)
├── ProtobufWriter           (最小 Protobuf エンコーダ)
├── ProtobufReader           (最小 Protobuf デコーダ)
├── EvenHubProto             (EvenHub プロトコル メッセージビルダ)
├── DevSettingsProto         (デバイス設定プロトコル メッセージビルダ)
├── G2SettingProto           (G2 設定プロトコル メッセージビルダ)
├── OnboardingProto          (オンボーディング メッセージビルダ)
├── EvenAIProto              (EvenAI メッセージビルダ)
├── MenuProto                (ダッシュボードメニュー メッセージビルダ)
├── EvenBLETransport         (BLE トランスポート層パケットビルダ)
├── G2SendManager            (送信管理: SyncId / MagicRandom 管理)
├── G2ReceiveManager         (受信管理: マルチパケット再組み立て)
├── G2ReconnectionManager    (再接続タイマー管理)
└── G2                       (メインクラス)
```

---

## BLE プロトコル仕様 (`G2BLE`)

G1 で使用されていた UART UUID とは異なる、EvenHub 専用の GATT 特性を使用する。

| 定数名 | UUID | 用途 |
|--------|------|------|
| `CHAR_WRITE` | `00002760-...-5401` | 書き込み用特性 |
| `CHAR_NOTIFY` | `00002760-...-5402` | 通知用特性 |
| `AUDIO_NOTIFY` | `00002760-...-6402` | 音声通知用特性 |
| `SERVICE_UUID` | `00002760-...-0000` | サービス UUID |
| `HEADER_BYTE` | `0xAA` | パケットヘッダマーカー |
| `MAX_PACKET_PAYLOAD` | 236 バイト | 1 パケットあたりの最大ペイロード |

---

## サービス ID 一覧 (`ServiceID`)

各サービスはプロトコルバッファのメッセージをルーティングするための識別子。

| 名前 | 値 | 用途 |
|------|----|------|
| `DASHBOARD` | `0x01` | ダッシュボード UI |
| `MENU` | `0x03` | メニュー UI |
| `EVEN_AI` | `0x07` | EvenAI 機能 |
| `G2_SETTING` | `0x09` | G2 デバイス設定 |
| `GESTURE_CTRL` | `0x0D` | ジェスチャーコントロール |
| `ONBOARDING` | `0x10` | オンボーディング |
| `DEVICE_SETTINGS` | `0x80` | デバイス設定 (UX) |
| `EVEN_HUB_CTRL` | `0x81` | EvenHub 制御チャネル |
| `EVEN_HUB` | `0xE0` | EvenHub メインチャネル |

---

## Protobuf 実装

外部ライブラリを使用せず、`ProtobufWriter` / `ProtobufReader` として手書きの最小実装を搭載。

### `ProtobufWriter` がサポートするフィールド型

| メソッド | Protobuf ワイヤ型 |
|----------|------------------|
| `writeVarint` | Varint (wire type 0) |
| `writeInt32Field` | int32 / enum |
| `writeStringField` | string (wire type 2) |
| `writeBytesField` | bytes (wire type 2) |
| `writeMessageField` | embedded message (wire type 2) |
| `writeBoolField` | bool (int32 として) |

### `ProtobufReader` の主な機能

- `readVarint` / `readTag` / `readInt32` / `readBytes` / `readString`
- `parseFields` によりフィールド番号をキーとした `Map<Int, Any>` を返す

---

## メッセージビルダ詳細

### `EvenHubProto`

EvenHub チャネル (`0xE0`) 向けのメッセージを構築。

| 関数 | コマンド | 説明 |
|------|----------|------|
| `createPageMessage` | `CREATE_STARTUP_PAGE (0)` | 新規ページ作成（テキスト/画像コンテナ） |
| `rebuildPageMessage` | `REBUILD_PAGE (7)` | 既存ページの再構築 |
| `updateTextMessage` | `UPDATE_TEXT_DATA (5)` | テキストコンテナ更新 |
| `updateImageRawDataMessage` | `UPDATE_IMAGE_RAW_DATA (3)` | 画像フラグメント送信 |
| `shutdownMessage` | `SHUTDOWN_PAGE (9)` | ページシャットダウン |
| `heartbeatMessage` | `HEARTBEAT (12)` | ハートビート |
| `audioControlMessage` | `AUDIO_CONTROL (15)` | マイク有効/無効 |

### `DevSettingsProto`

デバイス設定チャネル (`0x80`) 向け。認証・時刻同期・リング接続情報等。

| 関数 | コマンド ID | 説明 |
|------|-------------|------|
| `authCmd` | `AUTHENTICATION (4)` | BLE 認証 (secAuth=true, phoneType=Android) |
| `pipeRoleChange` | `PIPE_ROLE_CHANGE (5)` | パイプロール変更 (RIGHT=1) |
| `ringConnectInfo` | `RING_CONNECT_INFO (6)` | リング (コントローラ) 接続/切断 |
| `timeSync` | `TIME_SYNC (128)` | 時刻・タイムゾーン同期 |
| `baseHeartbeat` | `BASE_CONN_HEART_BEAT (14)` | 接続維持ハートビート |

### `G2SettingProto`

G2 設定チャネル (`0x09`) 向け。輝度・画面位置・ヘッドアップ設定。

| 関数 | 説明 |
|------|------|
| `setBrightness` | 輝度レベル・自動調整設定 |
| `requestInfo` | デバイス基本情報 (バッテリー/バージョン) 要求 |
| `setHeadUpSwitch` | ヘッドアップ表示の有効/無効 |
| `setHeadUpAngle` | ヘッドアップ表示角度 (0〜60) |
| `setScreenHeight` | 画面高さ位置レベル (0〜12) |
| `setScreenDepth` | 画面奥行き位置レベル (0〜2) |

### `MenuProto`

ダッシュボードメニュー (`0x03`) 向け。

- メニュー項目は最小 5 件・最大 10 件
- 先頭は必ず組み込み「Notification」(appId=4)
- 表示名は最大 15 文字に切り詰め
- `packageNameToAppId()`: パッケージ名 → `10029〜10534` の数値 appId へのハッシュ変換
- プレースホルダー appId: `10535〜10539`

---

## トランスポート層 (`EvenBLETransport`)

### パケット構造

```
[0]    HEADER_BYTE (0xAA)
[1]    SOURCE (phone=1) or DEST (glasses=2)  ← reserveFlag で切替
[2]    SyncId
[3]    PayloadLength
[4]    TotalPackets
[5]    SerialNum (1-indexed)
[6]    ServiceId
[7]    StatusByte (resultCode etc.)
[8..N] Payload
末尾2B CRC16 (最終パケットのみ)
```

- 最大 236 バイトのペイロードで分割送信
- 最終チャンクがちょうど 236 バイトの場合は CRC 専用の空パケットを追加

---

## `G2` クラス主要機能

### BLE 接続管理

- **左右独立 GATT 接続**: `leftGatt` / `rightGatt` で左右レンズを別々に管理
- **スキャン**: デバイス名またはシリアル番号でフィルタリング
- **アドレス保存**: `SharedPreferences` に左右アドレスを永続化し、再起動後も再接続
- **再接続**: `G2ReconnectionManager` により 30 秒間隔・無制限リトライ
- **ペアリングタイムアウト**: 10 秒以内に接続できなければタイムアウト通知

### 認証シーケンス

```
1. 左レンズに authCmd 送信
2. 右レンズに authCmd 送信
3. pipeRoleChange (RIGHT) 送信
4. timeSync 送信
5. requestInfo 送信 (バッテリー/バージョン取得)
6. EvenAI 設定 (HeyEven 無効化)
7. Onboarding スキップ
8. ダッシュボードシーケンス開始
```

### ディスプレイ制御

| メソッド | 説明 |
|----------|------|
| `sendTextWall(text)` | テキストをグラス画面全体に表示 |
| `sendDoubleTextWall(top, bottom)` | 上下 2 段テキスト表示 (`\n` 結合) |
| `clearDisplay()` | 表示クリア (shutdown + 再作成) |
| `displayBitmap(base64)` | ビットマップ画像を表示 |

### ビットマップ表示の仕組み (`displayBitmapQuad`)

```
入力画像 (任意サイズ)
    ↓ アスペクト比を保ちながら 400×200 にスケール
400×200 グレースケールビットマップ
    ↓ 4 タイル分割 (200×100 × 4)
[左上] [右上]
[左下] [右下]
    ↓ 各タイルを 4-bit BMP に変換 (16 色グレースケール)
    ↓ フラグメント分割 (4096 バイト/フラグメント)
    ↓ 1秒待機後にタイルを順次送信 (chained)
```

- BMP 形式: 4-bit (16 色グレースケールパレット)、DIB ヘッダ 40 バイト、下から上のピクセル並び
- フラグメント間は `BLE_PACKET_GAP_MS = 8ms` の間隔を空けてパケット欠落を防止

### ハートビート

| 種別 | 間隔 | 説明 |
|------|------|------|
| EvenHub ハートビート | 5 秒 | `HEARTBEAT` コマンド送信 |
| DevSettings ハートビート | 5 秒 | `BASE_CONN_HEART_BEAT` 送信 |
| テキストキュードレイン | 100 ms | 最新の保留テキストを 1 件送信 |
| バッテリーポーリング | 50 秒 (10 ハートビート毎) | `requestInfo` 送信 |

### イベント処理

`OsEventType` に対応するタッチジェスチャー:

| イベント | 説明 |
|----------|------|
| `CLICK (0)` | シングルタップ |
| `DOUBLE_CLICK (3)` | ダブルタップ |
| `SCROLL_TOP (1)` | 上スクロール |
| `SCROLL_BOTTOM (2)` | 下スクロール |
| `FOREGROUND_ENTER (4)` | アプリ前面表示 |
| `FOREGROUND_EXIT (5)` | アプリ背面移動 |
| `ABNORMAL_EXIT (6)` | 異常終了 |
| `SYSTEM_EXIT (7)` | システム終了 |

### 非サポート機能

G2 はカメラ・WiFi・RGB LED を搭載しないため、以下はスタブ実装:

- `requestPhoto` / `startStream` / `stopStream`
- `startVideoRecording` / `stopVideoRecording`
- `requestWifiScan` / `sendWifiCredentials` / `forgetWifiNetwork`
- `sendRgbLedControl`

---

## 設計上の特記事項

1. **外部 Protobuf ライブラリ不使用**: `ProtobufWriter` / `ProtobufReader` を手書きで実装。依存を最小化。

2. **GATT 操作キュー**: Android は GATT 操作を同時 1 件しか処理できないため、`gattOpQueue` でディスクリプタ書き込みを直列化。

3. **BLE パケット間隔**: `WRITE_TYPE_NO_RESPONSE` の連続送信時にパケットが欠落するため、マルチパケット送信では `BLE_PACKET_GAP_MS = 8ms` の間隔を挿入。

4. **左右レンズの分離受信**: `G2ReceiveManager` は `sourceKey` (LEFT/RIGHT) を含むキーでマルチパケットを管理し、左右の応答が混在しないよう保護。

5. **テキスト更新のバッファリング**: `queueEvenHubCommand` で最新メッセージのみを保持し、100ms ごとに 1 件だけ送信することで過負荷を防止。再送カウント (`EVEN_HUB_RESEND_COUNT = 1`) で最後のメッセージを 1 回再送。

6. **メニュー appId の決定論的ハッシュ**: パッケージ名から `10029〜10534` の範囲に確定的にマッピングすることで、再起動後もメニュー選択イベントを正確に逆引き可能。
