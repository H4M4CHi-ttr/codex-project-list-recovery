# Codex Project List Recovery for Windows

Windowsのクラッシュ後に、ChatGPTデスクトップアプリのCodexプロジェクト一覧だけが消えたときの復旧用スクリプトです。SQLiteに残っている登録を読み取り、一覧表示用JSONの2項目を再構成します。

対象は、**プロジェクトDBが正常で、`local-projects` が空になった特定の保存状態**です。SQLiteそのものの破損、チャット本文の消失、Windowsのクラッシュ原因を直すものではありません。OpenAIの公式ツールではありません。

まず「対応範囲」と「まとめて復旧する」を読んでください。診断コマンドや停止条件は、その後で確認できます。

## 対応範囲

| 項目 | 対象 |
| --- | --- |
| OS・アプリ | Windows、x64版のMicrosoft Storeパッケージ `OpenAI.Codex` |
| アプリの版番号 | `26.1016.6573.0`、`26.1021.3705.0` |
| Python | 3.11以上。追加のPythonパッケージは不要 |
| 保存先 | `CODEX_HOME`。未設定なら現在のユーザーの `.codex` |
| 復元する情報 | プロジェクトID、名前、ルートフォルダー、DBにある作成・更新日時、ID対応表 |

アプリの版番号に加え、`supported-builds.json` に記録したJavaScriptファイルのSHA-256を照合します。未確認の版や、同じ版番号でもファイルが異なるビルドは停止します。対応版の文字列だけを書き換えて実行しないでください。

アプリとスクリプトが同じ `CODEX_HOME` を使っていることを確認してください。WSL内のCodex、別ユーザーの保存先、複数の保存先の統合は対象外です。

## まとめて復旧する

このリポジトリ全体をZIPでダウンロードして展開するか、Gitでcloneします。ランチャー単体では動きません。

1. エクスプローラーから **`Restore-Codex-Projects.cmd`** をダブルクリックします。
2. 準備が完了して `Waiting: completely quit ChatGPT` が出たら、復旧用ウィンドウを残したままChatGPTを完全終了します。
3. **`SUCCESS: ... project registrations restored`** が出てから、ChatGPTを起動し直します。

ランチャーは、既存のアプリ同梱Python、`py`、`python` の順に実行環境を探します。同梱Pythonの場所が変わっていても、通常のPythonがあれば使えます。Pythonを自動インストールする処理はありません。

外部のPowerShellやコマンドプロンプトから実行する場合は、リポジトリのフォルダーで次を実行します。

```powershell
python codex_project_recovery.py recover
```

ChatGPT/Codexのツール経由では復旧処理を開始しません。そこから起動した待機プロセスも、アプリの終了に巻き込まれる場合があったためです。復旧用ウィンドウは完了するまで閉じないでください。アプリ終了を待つ時間は最大20分です。

## 診断や計画作成だけ行う

読み取り専用の診断では、JSONの形式、登録件数、SQLiteの整合性検査結果を表示します。チャット本文や認証情報を表示する処理はありません。

```powershell
python codex_project_recovery.py inspect
```

ライブの状態JSONを変更せず、バックアップと復元計画だけを作る場合は次を実行します。

```powershell
python codex_project_recovery.py prepare
```

`recovery-data/run-.../repair-plan.json` のパスが表示されます。その計画を使って復旧する場合は、次の `PLAN_PATH` を表示されたパスに置き換えてください。

```powershell
python codex_project_recovery.py restore "PLAN_PATH"
```

`inspect` と `prepare` はアプリ終了を要求しません。`restore` は外部の対話用コンソールから起動する必要があります。

## 変更するのはJSONの2項目

対象ファイルは `CODEX_HOME/.codex-global-state.json` です。

- `local-projects`
- `app-server-project-id-by-legacy-project-id-by-host`

プロジェクトDBは読み取り専用で開きます。DBの内容、チャット履歴、権限設定、プロジェクトのファイルは書き換えません。JSONのその他の値と、その項目の元のバイト列も保持します。

プロジェクトの作成・更新日時はDBの値を使います。失われたJSONだけにあったピン留め、表示順、配色、下書きなどは復元対象に含みません。

適用直前にアプリの停止、版番号と実装ハッシュ、DBの内容、計画の一致を再確認します。Windowsの排他ハンドルで状態ファイルを開き、直前の `before.json` と予定する内容を保存してから書き換え、読み戻して照合します。通常の書き込み例外では元のバイト列に戻します。

排他中のファイルを更新するため、状態JSONへの適用はファイル内の書き換えです。適用中にWindows自体が再び停止した場合や、記憶装置に障害がある場合まで復元を保証するものではありません。バックアップを保全して個別に調査してください。

`recovery-data/` には、保存先のパス、状態JSONのバックアップ、SQLiteのコピー、復元計画が入ります。**公開リポジトリやIssueへアップロードしないでください。** このフォルダーは `.gitignore` の対象です。

## 停止したときは上書きせず調べる

次の状態では適用しません。

| 状態 | 動作 |
| --- | --- |
| 未確認のアプリビルド・実装ハッシュ | 復旧を停止。`inspect` は利用可能 |
| SQLiteの整合性検査に失敗 | 復旧を停止。DBの修復は行わない |
| 一覧が空ではない、既存のID対応表がある | 上書きしない |
| JSONが不正、同名キーが重複 | 上書きしない |
| 元のプロジェクトIDが一意に決まらない | 新しいIDを推測しない |
| 追加のプロジェクトメタデータがある | ChatGPTとの関連付けなどを推測して戻さない |
| 準備後にDB・計画・アプリが変わった | 新しい計画が必要 |
| 本体・描画用・補助用などのアプリプロセスが残る | 終了を待つ。強制終了はしない |

障害報告用の `crashpad-handler` だけは停止待ちの対象から除きます。役割が分からないプロセスは、終了していないものとして扱います。

同じ保存先を変更する別のアプリやツールも止めてから適用してください。複数のDBとJSONをまとめたトランザクションにはなっていません。バックアップをライブファイルへ無条件で戻す処理もありません。

## 検証した範囲

元になった個人用スクリプトでは、2026年10月21日に `26.1016.6573.0` で26件の登録と一覧表示を復旧しました。`26.1021.3705.0` では実アプリのパーサーで26件の候補を検証し、26件を適用した記録を確認しています。

公開版ではユーザー固有のパス、固定件数、事前作成された計画を外しました。SQLiteとJSONの合成データによるテストを用意しています。元の個人用スクリプトの実績と、公開版による実データの復旧確認は別です。公開版を実データへ再適用するテストは行っていません。

アプリの内部保存形式や配布コード自体は同梱していません。収録しているのは、このリポジトリのPythonコード、ランチャー、テスト、説明書、照合用ハッシュです。

## 開発・問い合わせ

```powershell
python -m unittest discover -s tests -v
```

テストでは、一部だけ書き込んだ直後の例外、DBと計画の変更、起動元の拒否、Windowsの排他、未確認ビルドの拒否などを確認します。Windows以外ではWindows固有の排他テストをスキップします。

不具合は [Issues](https://github.com/H4M4CHi-ttr/codex-project-list-recovery/issues) へ報告してください。アプリの版番号、実行したコマンド、エラーメッセージを記載し、ユーザー名やパスは必要に応じて伏せてください。バックアップ、計画、チャット内容、認証ファイルの添付は不要です。

用語: `CODEX_HOME` はCodexの保存先、ID対応表はアプリ側のプロジェクトIDとDB側のIDを結び付ける情報です。SHA-256はファイルの内容を照合するために使います。

## English summary

A Windows-only, standard-library Python utility for a specific failure: the Codex desktop project list is empty while its SQLite project catalog remains intact. It reconstructs two JSON registration keys, with private backups, read-only SQLite access, app/build verification, external-console and process guards, exclusive file access, read-back verification, and rollback on ordinary write failures.

Only the two reviewed x64 Store builds listed above are accepted. This is an unofficial, version-specific recovery tool, not a SQLite repair tool or a fix for Windows crashes. Do not publish files from `recovery-data/`.

License: [MIT](LICENSE). 最終更新: 2026-10-28。
