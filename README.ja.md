# udfkit

**DVD-Video** と **Blu-ray** のディスクイメージを、Python だけで書き出すライブラリです。外部プログラムも管理者権限も要らず、標準ライブラリ以外に依存しません。

* **DVD-Video**（`udfkit.dvd`）：DVD プレーヤーや libdvdread（VLC・mpv）が読む UDF 1.02 + ISO 9660 の併用形式（ブリッジ）です。
  * VIDEO_TS のファイルは、IFO ファイルが示す位置に置きます。`genisoimage -dvd-video -udf` と同じ配置です。
  * そのため、genisoimage と同じ大きさ、同じセクタ配置のイメージになります。
* **Blu-ray**（`udfkit.bluray`）：BD-ROM が使う UDF 2.50 です（メタデータパーティション付き）。libbluray（libudfread 経由）が読める形式です。
* **ボリューム名**（`udfkit.info`）：イメージの ISO 9660 と UDF のボリューム名を読みます。

同じファイルと日時からは、毎回まったく同じバイト列のイメージができます。

## なぜ作ったか

Python の ISO ライブラリ pycdlib が書けるのは UDF 2.60 だけです。DVD プレーヤーが前提とするのは UDF 1.02、Blu-ray は 2.50 です。

他の方法には、それぞれ制約があります。

* 古いコマンドラインツール（mkisofs / genisoimage）を使う
* macOS 専用の `hdiutil` を使う
* マウントと管理者権限が要るツール（udftools）を使う

udfkit は Python が動く環境ならどこでも使えます。

## インストール

```sh
pip install git+https://github.com/kajisho5/udfkit
```

## 使い方

```sh
udfkit dvd    path/to/disc  disc.iso --label MY_DISC   # disc/ に VIDEO_TS（と AUDIO_TS・その他のファイル）
udfkit bluray path/to/bd    bd.iso   --label MY_BD     # bd/ に BDMV
udfkit info   disc.iso
```

```python
from udfkit import dvd, bluray

stats = dvd.write_image("disc", "disc.iso", "MY_DISC")
bluray.write_image("bd", "bd.iso", "MY_BD")
```

`dvd.write_image(..., shift=n)` を指定すると、VIDEO_TS の前に空のセクタを `n` 個置きます。2 層ディスクのレイヤーブレークを ECC ブロック（16 セクタ）の境界に合わせるためのものです。

## テスト

```sh
python -m unittest discover -s tests -t .
```

書き出し処理とは別に作った読み取り処理で、すべての構造を読み戻して確かめます。対象は、記述子のタグ・チェックサム・CRC と、UDF・ISO 9660 のディレクトリです。

次のツールが入っていれば、それを使った確認も行います。

* **isoinfo**：DVD イメージのファイル一覧と取り出し
* **libudfread**（libbluray が使う読み取りライブラリ）：Blu-ray イメージの全ファイルの読み戻し
* **ffmpeg + dvdauthor + genisoimage**：本物の VIDEO_TS から作ったイメージが、`genisoimage -dvd-video -udf` と同じ大きさ・同じセクタ配置になること

udfkit は、DVD・Blu-ray オーサリングアプリ [oncemore](https://github.com/kajisho5/oncemore) の一部として作られました。oncemore では、変更のたびにイメージを libdvdnav と libbluray で再生して確かめています。

## ライセンス

Apache License 2.0（[LICENSE](LICENSE) と [NOTICE](NOTICE) を参照）。
