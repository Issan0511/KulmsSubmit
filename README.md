# KULMS Submit

京大LMSの認証・課題要件確認・答案提出を扱う独立したMCP/CLIです。ノートアプリSpenNotesの内部Pythonモジュールには依存しません。


## セットアップ

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/kulms status --json
```

既に登録したID・パスワード・TOTP秘密鍵は再登録せず利用できます。OSキーストアのサービス名を維持しています。認証情報、答案、プロフィールはGitへ含めません。

ノート原稿のPDF化を使う場合は、SpenNotes側の公開コマンド`beamerplus-export`をPATHへ追加するか、`SPENNOTES_PYTHON=/absolute/BeamerPlus/.venv/bin/python`を設定します。既存PDFの提出にはノートアプリのインストールは不要です。

SpenNotes v2の公開データは`library/index.json`からノートの相対パス・ハッシュを読み、`courses/<科目>/<ノートID>/note.json`を検証します。起動中アプリにはloopbackの`save_note_snapshot`で保存を要求します。内部Pythonモジュールのimport、ノート原本の直接書き換えはありません。ノートに付属する資料は答案PDFには自動結合しません。必要な提出資料だけを別ファイルとして選びます。

テスト: `.venv/bin/python -m unittest discover -s tests`。LMS通信・送信は模擬応答で検証します。

## KULMSのOTP生成CLI

KULMSへのログインに使う認証コードをPC内で生成できます。TOTP秘密鍵のローカル登録・コード生成・登録状態の確認・削除に対応しています。後述の`kulms`は保存したOTPで自動ログインし、課題や教材を取得します。大学側でのTOTP登録と答案の提出は別の処理です。

```bash
.venv/bin/python -m pip install -e .
.venv/bin/kulms-otp setup
.venv/bin/kulms-otp status
.venv/bin/kulms-otp code
```

`setup`は自分のターミナルで実行し、非表示の入力欄に**大学側で登録済みのBase32秘密鍵**、または認証QRの内容である`otpauth://totp/...` URIを貼り付けます。KULMS+を設定済みなら、拡張機能の「コードを表示」で確認できる既存の秘密鍵を使えます。6桁の一時的なコードではありません。秘密鍵をチャットやコマンド引数へ貼り付ける必要はありません。大学側で新規登録中の場合は、その画面のシークレットを`setup`に保存してから、`code --min-validity 20`が表示する6桁を大学側の「ワンタイムパスワード」欄へ入力し、「登録」を押して完了させます。このCLIが認証アプリの代わりになるため、スマートフォンのアプリは必須ではありません。

KULMS+の[設定説明](https://radian0523.github.io/kulms-extension/2fa/)によると、大学の設定ページで新しく登録すると以前の秘密鍵は無効になります。このCLIの`setup`は既存の鍵をPCへ保存するだけで、大学の設定を変更しません。QR画像の読み取りは未実装です。

秘密鍵と生成方式はOSの資格情報ストア（LinuxのSecret Service／KWallet、macOSのKeychain、Windows Credential Locker）に保存します。Linuxでは利用可能なD-Busセッションとロック解除されたキーストアが必要です。保存先が利用できない場合はエラーで停止し、`.env`やノートのバックアップに保存しません。`status`は秘密鍵やコードを表示せず、登録状態と保存先を確認できます。

```bash
# 後のログイン処理から扱うJSON: code、valid_until（Unix秒）、remaining_seconds
.venv/bin/kulms-otp code --json
# 現在のコードを待機せず取得
.venv/bin/kulms-otp code --min-validity 0
# 秘密鍵のローカル設定を置き換える／削除する
.venv/bin/kulms-otp setup --replace
.venv/bin/kulms-otp remove
```

通常はコードだけを1行で返します。残り有効時間が5秒未満なら次の周期を待ち、待機後の時刻で再計算します（周期10秒未満の設定では半周期を基準にします）。Base32入力ではSHA1・6桁・30秒、URIでは明記されたSHA1／SHA256／SHA512・6桁／8桁・周期（1〜300秒）を使います。`--min-validity`は周期未満に指定してください。PCの時計を自動同期して使ってください。`code`以外の出力にはコードを含めません。コマンドの終了コードは成功0、設定・保存先などのエラー1、引数エラー2、中断130です。

CLIの代わりに`.venv/bin/python -m kulms_submit.KulmsOTPCLI`でも同じ操作ができます。ログイン処理からは`OTPStore().load()`と`current_code(config)`を呼び出せます。OTPの生成自体に通信はありません。

### ログイン用ID・パスワードの入力

ID・パスワードもOTPと同じOSのキーストアに保存できます。次を自分のターミナルで実行し、ECS-ID／SPS-ID、パスワード、確認用パスワードの順に入力します。入力はすべて非表示です。

```bash
.venv/bin/kulms setup
.venv/bin/kulms status
```

`status --json`は`login_configured`と`otp_configured`を返し、ID・パスワード・認証コードは表示しません。`.env`の編集は不要です。大学側のパスワードやFIDO／OTP設定は変更せず、既存のOTP秘密鍵も保持します。実際のログイン確認は`kulms login`で行います。

保存したID・パスワードを置き換えるときは`setup --replace`、ローカルのID・パスワードだけを削除するときは`remove`を使います。モジュールとしては`.venv/bin/python -m kulms_submit.KulmsLogin setup`でも実行できます。ログイン処理は`LoginStore().load()`で値を受け取れます。

### コース・課題・テスト・教材の取得

各コマンドはOSのキーストアから認証情報を読み、ID・パスワード、OTP、SAMLの順にログインします。FIDOの画面が出た場合は認証方式の選択に戻ってOTPを選びます。認証方式の記憶設定は送信しません。Cookieはそのコマンドのメモリ内だけで使い、コマンド終了時に破棄します。認証エラーでの自動再試行はありません。

```bash
.venv/bin/kulms login
.venv/bin/kulms courses
.venv/bin/kulms assignments --json
.venv/bin/kulms assignments --course COURSE_ID --json
.venv/bin/kulms tests --course COURSE_ID --json
.venv/bin/kulms materials --course COURSE_ID --json
.venv/bin/kulms download --course COURSE_ID --resource RESOURCE_ID --output lecture.pdf
```

`COURSE_ID`は`courses`の`id`、`RESOURCE_ID`は`materials`の`id`を使います。空白を含む値は引用符で囲んでください。教材APIには固有の`id`がないため、CLIは教材の`url`を`id`として追加します。ダウンロードは指定コースの一覧に実在するファイルが対象で、フォルダ・外部リンクは対象外です。保存先の親フォルダは事前に用意してください。既存ファイルは上書きせず、通信失敗時の途中ファイルは削除します。1ファイルの上限は512MiBです。

JSON出力は一覧の全フィールドを保持します。課題の説明・添付・提出状態などは`assignments --json`から取得できます。`assignments`は過去の課題も含み、未提出・締切前だけに限定しません。`tests`は公開テストの一覧取得だけを行い、受験開始や解答送信は行いません。答案の提出は次の`submit`を使い、本人の完成・提出指示を受けて実行します。

使用するAPIは次のとおりです。コースは50件ずつページ送りし、課題も50件ずつ取得します。テストと教材はコースごとの一覧を取得します。

| 対象 | パス |
| --- | --- |
| コース | `/direct/site.json` |
| 自分の課題 | `/direct/assignment/my.json` |
| コース別の課題 | `/direct/assignment/site/COURSE_ID.json` |
| 公開テスト | `/direct/sam_pub/context/COURSE_ID.json` |
| 教材 | `/direct/content/site/COURSE_ID.json` |
| 教材本体 | 教材一覧の`url`（LMS内の`/access/content/`） |

コースのページ送り・教材URLの扱いは[KULMS.Localの参照実装](https://github.com/HaruhiroObora/KULMS.Local/blob/32689734784ce1547e19cea28e5aadf629ef4cac/KULMS.Local/Services/KULMSApiService.cs)と[APIパスの設定](https://github.com/HaruhiroObora/KULMS.Local/blob/32689734784ce1547e19cea28e5aadf629ef4cac/KULMS.Local/Services/Settings.cs)を確認しました。Python実装はこのリポジトリ内にあります。

### 答案のインライン提出・ファイル提出

`submit`は既定で送信前の確認だけを行います。答案のファイル名・サイズ・SHA256、本文の文字数、提出先、以前の提出状態を表示し、アップロード・下書き保存・最終提出は行いません。課題IDは`assignments`の`id`を指定します。

```bash
# ファイル答案の送信前確認
.venv/bin/kulms submit --course COURSE_ID --assignment ASSIGNMENT_ID --file answer.pdf --json
# UTF-8の本文をインラインに入力する場合の送信前確認
.venv/bin/kulms submit --course COURSE_ID --assignment ASSIGNMENT_ID --text-file answer.txt --json
# 本人が完成・提出を指示したあとに実行
.venv/bin/kulms submit --course COURSE_ID --assignment ASSIGNMENT_ID --file answer.pdf --execute --json
```

本文とファイルは併用できます。`--file`は最大5回指定でき、合計64MiBまでです。大学側の上限がこれより小さい場合は大学側で拒否されます。本文はUTF-8で1MiBまでで、`--text-file`はHTMLの特殊文字をエスケープし、改行を保持します。HTML原稿を送る場合は`--html-file`を使います。両方の本文オプションは併用できません。本文オプションを省略した場合は、既存の本文を保持します。通常は既存添付を保持して新しいファイルを追加します。以前に提出した課題は、本人の再提出指示を受けて`--resubmit`も指定した場合だけ実行します。

既存のファイルを差し替える場合は、まずローカルへ保存して内容を照合してから、対象の`submittedAttachments[].ref`を`--replace-attachment REF`で指定します。送信前確認にも、削除するファイル名が表示されます。実行時はその添付の削除ボタンを確認してフォームから外し、新しいファイルをアップロードして提出します。最後に、指定した旧添付がなく、新しい添付が正しい内容で提出されたことを確認します。それ以外の既存添付は保持します。

課題ツールのIDはコースの`sitePages`から検出します。APIにツールIDが含まれないKULMSでは、「課題」ページを読み、そのフォームからツールIDを取得します。検出できない場合は`--page 'https://lms.gakusei.kyoto-u.ac.jp/portal/site/COURSE_ID/tool/TOOL_ID?panel=Main'`を指定できます。別コースや外部サイトのURLは受け付けません。本文のみ、本文と添付、添付のみ、および新規の単一ファイル提出に対応します。単一ファイル形式の既存答案の差し替え、グループ提出、教員による代理提出、追加の同意操作が必要な課題はブラウザで操作してください。

実行時は新しい課題フォームを取得し、そのCSRFトークンと大学のフォーム項目を使います。ファイル選択欄の`onchange`から読み取った`doAttachUpload`または`doAttachUploadSingle`へ`upload`という名前でmultipart送信し、添付された内容をダウンロードしてSHA256を照合します。本文の項目名は`Assignment.view_submission_text`です。続けて`eventSubmit_doRead_add_submission_form=x`と`option=confirm`を送り、確認画面の新しいトークンで`option=post`を送信します。同じ認証セッションを使い、この間に別の課題を開きません。

**`option=confirm`も下書きを保存する操作です。** 送信前確認ではこの操作を呼びません。`--execute`を指定するとアップロードから最終提出までを行います。最終提出後は課題APIを読み直し、今回の新しい受付時刻・提出済み状態・本文・新しい添付のSHA256・以前の添付の保持を検証してから成功を返します。HTTP 200だけでは成功にしません。通信切断などで結果が不明な場合は、送信を自動で繰り返さず、LMSの提出状態を確認するよう案内します。

KULMSの実フォームとJavaScriptに加え、[Sakai 23の提出処理](https://github.com/sakaiproject/sakai/blob/23.x/assignment/tool/src/java/org/sakaiproject/assignment/tool/AssignmentAction.java)と[確認画面](https://github.com/sakaiproject/sakai/blob/23.x/assignment/tool/src/webapp/vm/assignment/chef_assignments_student_confirm_submission.vm)を参照しています。添付答案は、本人の指示による実機での差し替え・再提出と、受付時刻・旧添付の除去・新添付のSHA256照合まで確認しています。インライン本文は模擬応答で書き込みを検証し、実機では送信前確認を行います。

### 要件確認を含む提出MCP

提出用のMCPはノート閲覧MCPとは独立して起動します。既存の認証・提出クライアントを共有し、追加のLLM/OCR API呼び出しや定期ポーリングは行いません。

```bash
.venv/bin/python -m kulms_submit.KulmsMCP --state-dir /absolute/private/kulms-state
```

Codexへの登録例です。Pythonと状態フォルダは絶対パスにしてください。

```bash
codex mcp add kulms -- /absolute/KulmsSubmit/.venv/bin/python -m kulms_submit.KulmsMCP --state-dir /absolute/private/kulms-state
```

認証・PDF化・受付確認を含む処理のため、`[mcp_servers.kulms]`の`tool_timeout_sec`には`180`を推奨します。設定方法は[公式MCPドキュメント](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)を参照してください。登録後はMCP接続を更新します。ノートの最新保存に対応するため、BeamerPlusも更新後に再起動してください。

通常の流れは以下のとおりです。各手順を呼ぶのはMCPにつながったアシスタントで、ユーザーが手動でJSONを作る必要はありません。

1. `list_kulms_courses`・`list_kulms_assignments`で提出先を確定し、`read_assignment_requirements`で実際の課題ページの指示を読む。
2. `list_submission_sources`から保存済みノートを選ぶか、答案ファイルのパスを指定する。ノートの既定の取得元は`BeamerPlus Notes/library/`（旧版の`notebooks.json`にも対応）。別の保存先は`BEAMERPLUS_NOTEBOOK_PATH`で設定する。
3. `prepare_submission`が、最新ノートの保存・全ページPDF化・答案コピー・要件資料のダウンロード・差し替える旧添付の退避・ファイル照合を行う。LMSへのアップロードや下書き保存はまだ行わない。`note_filename`または`output_names`で提出用コピーを命名でき、元ノート・元ファイルは変更しない。
4. `read_submission_artifact`で課題添付と提出する全ページを読み、アシスタントが内容を判断する。`record_submission_review`に、ファイル名・記名要件・設問への対応・可読性と、課題固有の各要件の出典および答案中の根拠を記録する。未確認ページ、不足、判読困難、矛盾、未対応形式は合格にしない。ハッシュやページ数だけで内容を確認した扱いにはしない。
5. 本人の完成・提出指示を受けたら`submit_reviewed_assignment`を呼ぶ。既にその答案の提出を依頼されていれば、同じ確認を繰り返す必要はない。課題・元ノート・答案コピー・プロフィール・既存提出の変更を再確認し、問題がなければアップロードから最終提出、受付と内容一致の検証まで行う。

`set_student_profile`で本人から提供された氏名・学籍番号・所属をローカル保存できます。プロフィールは記名が必要な課題で参照し、自動挿入はしません。`anonymousGrading`はLMSの採点設定なので、それだけで氏名禁止とは判断せず、課題本文や添付の指定と合わせて確認します。

確認結果は課題要件・配布資料・答案の内容ハッシュに結び付きます。同一内容の準備では過去の確認を再利用し、ノートのPDF化も内容が同じならキャッシュを使います。変更があれば再確認が必要です。内容確認は接続中のアシスタントが担当し、MCPが別モデルを起動することはありません。アシスタントは不明点を`uncertain`または`blocked`として残し、解消するまで送信しません。新しい会話で過去の資料が文脈にない場合は、正しく判断するために必要な部分を読み直します。

状態は`prepared`→`reviewed`→`submitting`→`submitted`です。不足は`blocked`、書き込み開始後に結果を確定できなければ`uncertain`になります。実行中ロックと受付の保存により、同じ準備IDの重複実行を防ぎます。成功済みのIDをもう一度送ると保存済み受付を返します。通信切断後は自動再送しません。`read_submission_status`でローカルの状態と受付を取得できます。

起動中のノートアプリが保存に失敗・筆記中・旧バージョンの場合、古い保存ファイルに黙って切り替えて送信しません。アプリが閉じていれば保存済みノートを使います。PDF・画像・UTF-8テキストを確認できます。PDFは1資料200ページ、課題と既存答案の参照添付は30件・合計128MiBまでです。外部リンク等でMCPが取得しない資料も、アシスタントの要件確認では未確認のまま合格にしないでください。

状態フォルダにはプロフィール、確認用答案・課題資料、確認記録、受付を保存します。既定はアプリデータ内の`KULMS Submit/`で、`KULMS_HOME`（旧`BEAMERPLUS_KULMS_HOME`も可）または`--state-dir`で変更できます。このリポジトリでは`data/`と`state/`がGit除外済みです。認証の秘密鍵やCSRF付きの生HTMLはこのフォルダへ保存しません。
