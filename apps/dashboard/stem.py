"""apps/dashboard/stem.py — STEM for Space Weather（12–18 歲教學頁）。

**設計對象**：國中到高中生。這個年齡層看得懂比例、圖表與因果推理，
但看不懂「準對數尺度」「環電流」這類術語。所以策略是：

  用類比取代術語   磁場 → 雨傘；CME → 太陽打嗝；Kp → 地震震度
  用可比的數字     8 分 20 秒 → 比下課時間還短；1 AU → 光要走 8 分鐘
  用真實資料       頁面顯示的是**此刻**的太陽，不是教科書插圖
  用互動取代閱讀   三個小遊戲，答錯會解釋為什麼

**刻意不簡化的兩件事**：
  · 不把模式輸出說成觀測（圖說仍標明哪些是電腦算出來的）
  · 不誇大危險（太空天氣不會「毀滅地球」，講清楚實際影響尺度）
  教育推廣最容易犯的錯就是為了吸引注意而誇大，反而讓學生日後發現被騙。

**多語**：繁體中文、日本語、English、Bahasa Melayu。
翻譯與內容同檔維護——分檔會讓其中一種語言悄悄過期。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import streamlit as st

LANGS = {
    "繁體中文": "zh",
    "日本語": "ja",
    "English": "en",
    "Bahasa Melayu": "ms",
}

# ── 文案 ────────────────────────────────────────────────────────────────
T: dict[str, dict[str, str]] = {
    "title": {
        "zh": "STEM：太空天氣入門",
        "ja": "STEM：宇宙天気入門",
        "en": "STEM: Space Weather Basics",
        "ms": "STEM: Asas Cuaca Angkasa",
    },
    "subtitle": {
        "zh": "給 12–18 歲。這一頁顯示的是**此刻**的太陽，不是課本插圖。",
        "ja": "12〜18歳向け。このページに映るのは**今この瞬間**の太陽です。教科書の図ではありません。",
        "en": "For ages 12–18. What you see here is the Sun **right now** — not a textbook diagram.",
        "ms": "Untuk umur 12–18. Apa yang anda lihat di sini ialah Matahari **sekarang** — bukan gambar buku teks.",
    },
    "s1_head": {
        "zh": "一、太陽不只是會發光",
        "ja": "一、太陽は光るだけではない",
        "en": "1. The Sun does more than shine",
        "ms": "1. Matahari bukan sekadar bersinar",
    },
    "s1_body": {
        "zh": """太陽是一顆巨大的**電漿球**——氣體熱到電子被扯離原子，整團帶電。
帶電的東西流動就會產生磁場，所以太陽有磁場，而且很亂。

磁力線有時會扭在一起、突然「啪」地重新接上（叫做**磁重聯**），
把累積的能量在幾分鐘內全部放出來。這就是**太陽閃焰**。

有時太陽還會把一大團電漿整個拋出來，像打了個嗝——這叫 **CME**（日冕物質拋射）。

太陽表面比較暗的斑點是**黑子**，那裡磁場特別強。
黑子多的時候，閃焰和 CME 也多。""",
        "ja": """太陽は巨大な**プラズマの球**です。気体が熱すぎて電子が原子から引きはがされ、全体が電気を帯びています。
電気を帯びたものが流れると磁場ができるので、太陽には磁場があります。しかもかなり複雑です。

磁力線がねじれて、突然つなぎ直ることがあります（**磁気リコネクション**）。
このとき、たまっていたエネルギーが数分で一気に放出されます。これが**太陽フレア**です。

さらに、プラズマのかたまりを丸ごと吹き飛ばすこともあります。ゲップのようなものです。これを **CME**（コロナ質量放出）といいます。

太陽表面の暗いしみは**黒点**で、磁場が特に強い場所です。
黒点が多い時期は、フレアや CME も多くなります。""",
        "en": """The Sun is a giant ball of **plasma** — gas so hot that electrons get torn off atoms, leaving everything electrically charged.

Moving charges make magnetic fields, so the Sun has one. A very tangled one.

Sometimes those magnetic field lines twist, then suddenly snap and reconnect (**magnetic reconnection**), dumping stored energy in minutes. That's a **solar flare**.

Sometimes the Sun also throws out a whole blob of plasma — like a burp. That's a **CME** (coronal mass ejection).

The dark spots on the Sun's surface are **sunspots**, where the magnetic field is strongest. More sunspots means more flares and CMEs.""",
        "ms": """Matahari ialah bola **plasma** gergasi — gas yang begitu panas sehingga elektron tertanggal daripada atom, menjadikan semuanya bercas elektrik.

Cas yang bergerak menghasilkan medan magnet, jadi Matahari mempunyai medan magnet. Yang sangat kusut.

Kadangkala garis medan magnet itu berpintal, kemudian tiba-tiba putus dan bersambung semula (**penyambungan semula magnet**), melepaskan tenaga tersimpan dalam beberapa minit. Itulah **suar suria**.

Kadangkala Matahari juga melontarkan segumpal plasma — seperti bersendawa. Itulah **CME** (lontaran jisim korona).

Tompok gelap di permukaan Matahari ialah **tompok matahari**, tempat medan magnet paling kuat. Lebih banyak tompok bermakna lebih banyak suar dan CME.""",
    },
    "s2_head": {
        "zh": "二、三種東西會飛過來，速度差很多",
        "ja": "二、飛んでくるものは三種類。速さがまるで違う",
        "en": "2. Three things come at us — at wildly different speeds",
        "ms": "2. Tiga benda datang kepada kita — pada kelajuan yang jauh berbeza",
    },
    "s2_body": {
        "zh": """這是整個太空天氣裡**最重要的一件事**：

| 是什麼 | 多久到地球 | 能不能提前知道 |
|---|---|---|
| **光**（X 射線、紫外線） | **8 分 20 秒** | 不行。看到的時候已經打到了 |
| **高能質子** | 幾十分鐘～幾小時 | 只有一點點時間 |
| **CME 電漿雲** | **1～3 天** | 可以，這是預警的主要機會 |

為什麼差這麼多？光以光速跑；質子接近光速；CME 是一大團物質，
「只有」每秒幾百到兩千公里——聽起來很快，但太陽到地球有 1.5 億公里。

**所以「閃焰預警」幾乎不可能，「地磁暴預警」才做得到。**""",
        "ja": """これが宇宙天気で**いちばん大事なこと**です：

| 何が | 地球まで | 事前に分かる？ |
|---|---|---|
| **光**（X線・紫外線） | **8分20秒** | 無理。見えた時にはもう届いている |
| **高エネルギー陽子** | 数十分〜数時間 | ほんの少しだけ |
| **CME のプラズマ雲** | **1〜3日** | できる。予報の主なチャンス |

なぜこんなに違うのか。光は光速、陽子はほぼ光速。
CME は物質のかたまりなので「たった」秒速数百〜2000km です。
速そうですが、太陽から地球までは1億5000万kmもあります。

**だから「フレア予報」はほぼ不可能で、「磁気嵐の予報」はできるのです。**""",
        "en": """This is the **single most important idea** in space weather:

| What | Time to Earth | Can we warn ahead? |
|---|---|---|
| **Light** (X-rays, UV) | **8 min 20 s** | No. By the time you see it, it has hit |
| **High-energy protons** | Tens of minutes to hours | Only a little |
| **CME plasma cloud** | **1–3 days** | Yes — this is the main warning chance |

Why so different? Light travels at light speed; protons nearly so.
A CME is a lump of matter moving at "only" a few hundred to 2000 km per second.
That sounds fast, but the Sun is 150 million km away.

**So "flare warning" is nearly impossible, while "geomagnetic storm warning" is doable.**""",
        "ms": """Ini idea **paling penting** dalam cuaca angkasa:

| Apa | Masa ke Bumi | Boleh beri amaran awal? |
|---|---|---|
| **Cahaya** (sinar-X, UV) | **8 minit 20 saat** | Tidak. Bila anda nampak, ia sudah sampai |
| **Proton bertenaga tinggi** | Puluhan minit hingga jam | Sedikit sahaja |
| **Awan plasma CME** | **1–3 hari** | Ya — inilah peluang amaran utama |

Mengapa berbeza? Cahaya bergerak pada laju cahaya; proton hampir sama.
CME ialah ketulan jirim yang bergerak pada "hanya" beberapa ratus hingga 2000 km sesaat.
Kedengaran laju, tetapi Matahari berjarak 150 juta km.

**Jadi "amaran suar" hampir mustahil, manakala "amaran ribut geomagnet" boleh dilakukan.**""",
    },
    "s3_head": {
        "zh": "三、地球有一把「磁場雨傘」",
        "ja": "三、地球には「磁場の傘」がある",
        "en": "3. Earth has a magnetic umbrella",
        "ms": "3. Bumi mempunyai payung magnet",
    },
    "s3_body": {
        "zh": """地球本身是個大磁鐵，磁場向外撐開成一個保護罩，把大部分帶電粒子擋掉。

但這把傘不是完美的：

- **CME 的磁場如果朝南**，正好跟地球磁場方向相反，兩者會「接上」，
  能量就大量灌進來 → **地磁暴**。
- **如果朝北**，方向相同、互相排斥，多半沒事。

所以太空天氣預報員最在意的一個數字叫 **Bz**——它就是「CME 的磁場朝南還是朝北」。

記一句話：**朝南才會出事。**

粒子沿磁力線衝進南北極的大氣，撞擊空氣分子讓它發光——那就是**極光**。""",
        "ja": """地球そのものが大きな磁石で、磁場が外へ広がって防護シールドになり、荷電粒子の多くをはじき返します。

でも、この傘は完璧ではありません：

- **CME の磁場が南向き**だと、地球の磁場と逆向きなので「つながって」しまい、
  エネルギーが大量に流れ込みます → **磁気嵐**。
- **北向き**なら同じ向きで反発するので、たいてい何も起きません。

だから宇宙天気予報士がいちばん気にする数字が **Bz**、つまり「CME の磁場が南向きか北向きか」です。

覚え方：**南向きだとまずい。**

粒子が磁力線に沿って極域の大気に飛び込み、空気の分子にぶつかって光る——それが**オーロラ**です。""",
        "en": """Earth itself is a big magnet. Its field spreads outward into a shield that deflects most charged particles.

But the umbrella isn't perfect:

- **If the CME's magnetic field points south**, it is opposite to Earth's field. The two "connect," and energy pours in → **geomagnetic storm**.
- **If it points north**, the fields align and repel. Usually nothing happens.

That's why forecasters watch one number above all: **Bz** — whether the incoming field points south or north.

Remember: **southward means trouble.**

Particles funnel down the field lines into the polar atmosphere and hit air molecules, making them glow — that's the **aurora**.""",
        "ms": """Bumi sendiri ialah magnet besar. Medannya merebak ke luar menjadi perisai yang menepis kebanyakan zarah bercas.

Tetapi payung ini tidak sempurna:

- **Jika medan magnet CME menghala ke selatan**, ia bertentangan dengan medan Bumi. Kedua-duanya "bersambung", dan tenaga mencurah masuk → **ribut geomagnet**.
- **Jika menghala ke utara**, medan sejajar dan menolak. Biasanya tiada apa berlaku.

Sebab itulah peramal memerhati satu nombor melebihi segalanya: **Bz** — sama ada medan yang masuk menghala ke selatan atau utara.

Ingat: **ke selatan bermakna masalah.**

Zarah menyusur garis medan ke atmosfera kutub dan melanggar molekul udara, menyebabkannya bercahaya — itulah **aurora**.""",
    },
    "s4_head": {
        "zh": "四、那會影響我們什麼？",
        "ja": "四、それで、私たちに何が起きる？",
        "en": "4. So what does it actually do to us?",
        "ms": "4. Jadi apa kesannya kepada kita?",
    },
    "s4_body": {
        "zh": """先講清楚：**太空天氣不會毀滅地球，也不會直接傷害地面上的人。**
大氣層和磁場擋掉了危險的部分。真正受影響的是**科技系統**：

- **GPS 定位變不準**——電離層被擾動，訊號穿過時被延遲。嚴重時手機導航會飄幾公尺到幾十公尺。
- **短波無線電中斷**——飛機飛極地航線、船舶、業餘無線電會突然聯絡不上。
- **衛星掉高**——大氣受熱膨脹，低軌衛星阻力變大，掉得比預期快。
  2022 年 SpaceX 有一批 Starlink 衛星就因此沒能進入軌道。
- **電網**——極端情況下會在長距離電線裡感應出電流，1989 年魁北克曾因此大停電。

臺灣有個特別的地方：我們的**地磁緯度只有約 19°N**（不是地理的 23.5°N），
正好落在電離層最不穩定的「赤道異常」區。所以**臺灣的 GPS 受影響其實比想像中大**。""",
        "ja": """まずはっきりさせておきます：**宇宙天気で地球が滅びることはないし、地上の人が直接傷つくこともありません。**
大気と磁場が危険な部分を防いでいます。影響を受けるのは**技術システム**です：

- **GPS の精度が落ちる**——電離圏が乱れ、信号が遅れます。ひどいと数メートル〜数十メートルずれます。
- **短波通信が途切れる**——極域を飛ぶ航空機、船舶、アマチュア無線が急に通じなくなります。
- **衛星が落ちる**——大気が熱で膨張し、低軌道衛星の抵抗が増えて予想より速く高度を失います。
  2022年、SpaceX のスターリンク衛星がこれで軌道に入れませんでした。
- **電力網**——極端な場合、長い送電線に電流が誘導されます。1989年のケベック大停電が有名です。

台湾には特別な事情があります。**地磁気緯度が約19°N**（地理的な23.5°Nではない）で、
電離圏が最も不安定な「赤道異常」帯に入ります。つまり**台湾の GPS は思ったより影響を受けやすい**のです。""",
        "en": """First, to be clear: **space weather will not destroy Earth, and it does not directly harm people on the ground.**
The atmosphere and magnetic field block the dangerous parts. What gets affected is **technology**:

- **GPS accuracy drops** — the ionosphere gets disturbed and delays the signal. In bad events your phone's position can drift by metres to tens of metres.
- **Shortwave radio blacks out** — aircraft on polar routes, ships, and ham radio suddenly lose contact.
- **Satellites lose altitude** — the atmosphere heats and puffs up, so low-orbit satellites feel more drag and fall faster than predicted. In 2022 a batch of SpaceX Starlink satellites failed to reach orbit for exactly this reason.
- **Power grids** — in extreme cases currents get induced in long transmission lines. Quebec had a famous blackout in 1989.

Taiwan has a special situation: our **geomagnetic latitude is only about 19°N** (not the geographic 23.5°N), which puts us in the "equatorial anomaly" belt where the ionosphere is most unstable. So **GPS in Taiwan is affected more than you would guess**.""",
        "ms": """Pertama, untuk jelas: **cuaca angkasa tidak akan memusnahkan Bumi, dan tidak membahayakan manusia di darat secara langsung.**
Atmosfera dan medan magnet menghalang bahagian berbahaya. Yang terjejas ialah **teknologi**:

- **Ketepatan GPS merosot** — ionosfera terganggu dan melambatkan isyarat. Dalam peristiwa teruk, kedudukan telefon anda boleh terpesong beberapa meter hingga puluhan meter.
- **Radio gelombang pendek terputus** — pesawat di laluan kutub, kapal, dan radio amatur tiba-tiba hilang hubungan.
- **Satelit kehilangan ketinggian** — atmosfera memanas dan mengembang, jadi satelit orbit rendah mengalami seretan lebih dan jatuh lebih cepat daripada jangkaan. Pada 2022, sekumpulan satelit Starlink SpaceX gagal mencapai orbit atas sebab ini.
- **Grid kuasa** — dalam kes ekstrem, arus teraruh dalam talian penghantaran panjang. Quebec mengalami gangguan bekalan terkenal pada 1989.

Taiwan mempunyai keadaan istimewa: **latitud geomagnet kita hanya sekitar 19°N** (bukan 23.5°N geografi), meletakkan kita dalam jalur "anomali khatulistiwa" di mana ionosfera paling tidak stabil. Jadi **GPS di Taiwan terjejas lebih daripada yang anda sangka**.""",
    },
    "s5_head": {
        "zh": "五、現在的太陽長怎樣？",
        "ja": "五、今の太陽はどうなっている？",
        "en": "5. What does the Sun look like right now?",
        "ms": "5. Bagaimana rupa Matahari sekarang?",
    },
    "live_note": {
        "zh": "以下是**此刻**的真實觀測值。頁面每次開啟都會更新。",
        "ja": "以下は**今この瞬間**の実測値です。ページを開くたびに更新されます。",
        "en": "These are **real measurements from right now**. They update every time you open this page.",
        "ms": "Ini ialah **ukuran sebenar dari sekarang**. Ia dikemas kini setiap kali anda buka halaman ini.",
    },
    "no_data": {
        "zh": "目前沒有這項資料。**沒資料不等於沒事**——這是科學工作很重要的分辨。",
        "ja": "今このデータはありません。**データが無いことと、異常が無いことは違います**——科学ではとても大事な区別です。",
        "en": "No data for this right now. **No data is not the same as no problem** — an important distinction in science.",
        "ms": "Tiada data buat masa ini. **Tiada data tidak sama dengan tiada masalah** — perbezaan penting dalam sains.",
    },
    "games_head": {
        "zh": "六、小遊戲",
        "ja": "六、ミニゲーム",
        "en": "6. Mini-games",
        "ms": "6. Permainan mini",
    },
    "g1_title": {
        "zh": "遊戲 1　誰先到？",
        "ja": "ゲーム1　どれが先に着く？",
        "en": "Game 1　Which arrives first?",
        "ms": "Permainan 1　Yang mana sampai dahulu?",
    },
    "g1_q": {
        "zh": "太陽現在同時發生閃焰、放出質子、拋出 CME。**哪一個最先抵達地球？**",
        "ja": "太陽で今、フレアと陽子放出と CME が同時に起きました。**どれが最初に地球へ届く？**",
        "en": "The Sun just produced a flare, a burst of protons, and a CME at the same moment. **Which reaches Earth first?**",
        "ms": "Matahari baru menghasilkan suar, semburan proton, dan CME pada masa yang sama. **Yang mana sampai ke Bumi dahulu?**",
    },
    "g2_title": {
        "zh": "遊戲 2　你來當預報員",
        "ja": "ゲーム2　君が予報士",
        "en": "Game 2　You be the forecaster",
        "ms": "Permainan 2　Anda jadi peramal",
    },
    "g2_intro": {
        "zh": "看太陽風的資料，判斷會不會有地磁暴。**關鍵是 Bz 的正負**。",
        "ja": "太陽風のデータを見て、磁気嵐が来るか判断しよう。**カギは Bz の符号**です。",
        "en": "Look at the solar wind data and decide whether a storm is coming. **The key is the sign of Bz.**",
        "ms": "Lihat data angin suria dan tentukan sama ada ribut akan datang. **Kuncinya ialah tanda Bz.**",
    },
    "g3_title": {
        "zh": "遊戲 3　等級對對碰",
        "ja": "ゲーム3　レベルあてクイズ",
        "en": "Game 3　Match the scale",
        "ms": "Permainan 3　Padankan skala",
    },
    "g3_intro": {
        "zh": "地磁暴用 **G1～G5** 分級，依據是 **Kp 指數**（0～9）。Kp 5 是 G1，之後每加 1 就升一級。",
        "ja": "磁気嵐は **G1〜G5** で分類し、**Kp指数**（0〜9）で決まります。Kp 5 が G1 で、1上がるごとに1段階上がります。",
        "en": "Geomagnetic storms are graded **G1–G5** from the **Kp index** (0–9). Kp 5 is G1, and each step of 1 raises the level.",
        "ms": "Ribut geomagnet digredkan **G1–G5** daripada **indeks Kp** (0–9). Kp 5 ialah G1, dan setiap kenaikan 1 menaikkan satu tahap.",
    },
    "check": {"zh": "看答案", "ja": "答えを見る", "en": "Check answer", "ms": "Semak jawapan"},
    "next": {"zh": "下一題", "ja": "次の問題", "en": "Next question", "ms": "Soalan seterusnya"},
    "correct": {"zh": "答對了！", "ja": "正解！", "en": "Correct!", "ms": "Betul!"},
    "wrong": {"zh": "再想想", "ja": "もう一度考えてみよう", "en": "Not quite", "ms": "Belum tepat"},
    "score": {"zh": "答對", "ja": "正解数", "en": "Score", "ms": "Skor"},
    "reset": {"zh": "重新開始", "ja": "リセット", "en": "Reset", "ms": "Set semula"},
    "more_head": {
        "zh": "想更深入？",
        "ja": "もっと知りたい？",
        "en": "Want to go deeper?",
        "ms": "Mahu mendalami lagi?",
    },
    "more_body": {
        "zh": """- 這個系統的其他頁面顯示的是專業判讀用的資料，你已經看得懂大部分了。
- 「名詞與判讀」頁有更完整的名詞解釋。
- 想自己算算看？太陽到地球 1.5 億公里，光速每秒 30 萬公里——驗算一下 8 分 20 秒對不對。
- 所有影像都來自 NASA 與 NOAA 的公開資料，每張圖下面都標了來源。""",
        "ja": """- このシステムの他のページは専門家向けのデータですが、もうかなり読めるはずです。
- 「名詞與判讀」ページにもっと詳しい用語解説があります。
- 自分で計算してみる？ 太陽まで1億5000万km、光速は秒速30万km——8分20秒が合っているか確かめてみよう。
- 画像はすべて NASA と NOAA の公開データです。各画像の下に出典を明記しています。""",
        "en": """- The other pages in this system show data meant for professionals — you can already read most of it.
- The "名詞與判讀" page has fuller definitions.
- Want to check the maths? The Sun is 150 million km away and light travels 300,000 km per second — verify the 8 min 20 s for yourself.
- All images come from public NASA and NOAA data, with the source credited under each one.""",
        "ms": """- Halaman lain dalam sistem ini memaparkan data untuk profesional — anda sudah boleh membaca kebanyakannya.
- Halaman "名詞與判讀" mempunyai definisi yang lebih lengkap.
- Mahu semak pengiraan? Matahari berjarak 150 juta km dan cahaya bergerak 300,000 km sesaat — sahkan sendiri 8 minit 20 saat itu.
- Semua imej daripada data awam NASA dan NOAA, dengan sumber dinyatakan di bawah setiap satu.""",
    },
}



# ── 影像／動畫的多語說明 ────────────────────────────────────────────────
# **不能沿用 configs/imagery.yaml 的 note**：那是寫給值勤人員的中文操作說明，
# 不會隨語言切換，而且用語對 12–18 歲太專業。
# 這裡另備一套教學用說明，四語齊備（有測試守住缺一即紅燈）。
# 產製者名稱維持原文（專有名詞），只翻譯標籤與條款摘要。
MEDIA: dict[str, dict[str, dict[str, str]]] = {
    "sdo_white_light": {
        "title": {
            "zh": "此刻的太陽（白光）",
            "ja": "今の太陽（可視光）",
            "en": "The Sun right now (visible light)",
            "ms": "Matahari sekarang (cahaya nampak)",
        },
        "note": {
            "zh": "暗斑就是黑子。數數看現在有幾組——黑子多的時候，閃焰也多。",
            "ja": "暗いしみが黒点です。今いくつあるか数えてみよう。黒点が多い時期はフレアも多くなります。",
            "en": "The dark spots are sunspots. Count how many groups you can see — more sunspots means more flares.",
            "ms": "Tompok gelap ialah tompok matahari. Kira berapa kumpulan yang anda nampak — lebih banyak tompok bermakna lebih banyak suar.",
        },
    },
    "sdo_euv_304": {
        "title": {
            "zh": "色球層（紫外光 304Å）",
            "ja": "彩層（紫外線 304Å）",
            "en": "The chromosphere (UV 304Å)",
            "ms": "Kromosfera (UV 304Å)",
        },
        "note": {
            "zh": "約 5 萬度的氣層。邊緣伸出去的環狀結構是日珥，它爆發時常伴隨 CME。",
            "ja": "約5万度の層です。ふちから伸びる輪のような構造がプロミネンス（紅炎）で、爆発するとCMEを伴うことがよくあります。",
            "en": "A layer at about 50,000°C. The loops reaching off the edge are prominences — when they erupt, a CME often follows.",
            "ms": "Lapisan pada kira-kira 50,000°C. Gelung yang menjulur di tepi ialah prominens — apabila ia meletus, CME sering menyusul.",
        },
    },
    "soho_lasco_c2": {
        "title": {
            "zh": "日冕儀（遮住太陽才看得到）",
            "ja": "コロナグラフ（太陽を隠して見る）",
            "en": "Coronagraph (blocking the Sun to see around it)",
            "ms": "Koronagraf (menutup Matahari untuk melihat sekelilingnya)",
        },
        "note": {
            "zh": "中間的圓盤是人造遮罩，擋住刺眼的日面。向外擴張的亮弧就是 CME。",
            "ja": "中央の円盤は人工のマスクで、まぶしい太陽面を隠しています。外へ広がる明るい弧がCMEです。",
            "en": "The disc in the middle is an artificial mask hiding the blinding solar surface. The bright arcs expanding outward are CMEs.",
            "ms": "Cakera di tengah ialah topeng buatan yang menutup permukaan Matahari yang menyilaukan. Lengkok terang yang mengembang ke luar ialah CME.",
        },
    },
    "swpc_ovation_north": {
        "title": {
            "zh": "極光橢圓（北半球）",
            "ja": "オーロラオーバル（北半球）",
            "en": "The auroral oval (northern hemisphere)",
            "ms": "Bujur aurora (hemisfera utara)",
        },
        "note": {
            "zh": "**這是電腦算的，不是照片。** 擾動越強，這個環就往赤道擴張得越低。",
            "ja": "**これはコンピュータの計算結果で、写真ではありません。** 擾乱が強いほど、この輪は赤道側へ広がります。",
            "en": "**This is a computer model, not a photograph.** The stronger the disturbance, the further this ring spreads toward the equator.",
            "ms": "**Ini model komputer, bukan gambar foto.** Lebih kuat gangguan, lebih jauh cincin ini merebak ke arah khatulistiwa.",
        },
    },
    "sdo_304_video": {
        "title": {
            "zh": "太陽是活的（動畫）",
            "ja": "太陽は生きている（動画）",
            "en": "The Sun is alive (video)",
            "ms": "Matahari itu hidup (video)",
        },
        "note": {
            "zh": "近兩天的連續影像。看邊緣的日珥怎麼升起、扭轉、噴出去——靜態圖看不到這些。",
            "ja": "ここ2日ほどの連続画像です。ふちのプロミネンスが立ち上がり、ねじれ、噴き出す様子を見てみよう。静止画では分かりません。",
            "en": "Two days of continuous images. Watch the prominences at the edge rise, twist, and erupt — a still image can't show this.",
            "ms": "Dua hari imej berterusan. Perhatikan prominens di tepi naik, berpintal, dan meletus — imej pegun tidak dapat menunjukkan ini.",
        },
    },
    "soho_c2_video": {
        "title": {
            "zh": "CME 噴發（動畫）",
            "ja": "CMEの噴出（動画）",
            "en": "A CME erupting (video)",
            "ms": "CME meletus (video)",
        },
        "note": {
            "zh": "動畫才分得出 CME 是往我們噴還是往旁邊噴。**環繞整圈的（暈狀）就是朝著地球來的。**",
            "ja": "CMEがこちらへ向かっているのか横へそれているのかは、動画でないと分かりません。**全体を取り囲むように見える（ハロー型）ものが地球方向です。**",
            "en": "Only in motion can you tell whether a CME is heading toward us or off to the side. **One that surrounds the whole disc (a halo) is coming at Earth.**",
            "ms": "Hanya dalam gerakan anda dapat tahu sama ada CME menuju ke arah kita atau ke tepi. **Yang mengelilingi seluruh cakera (halo) sedang menuju ke Bumi.**",
        },
    },
}

SOURCE_LABEL = {"zh": "來源", "ja": "出典", "en": "Source", "ms": "Sumber"}
LOAD_FAIL = {
    "zh": "影像無法載入", "ja": "画像を読み込めません",
    "en": "Image could not load", "ms": "Imej tidak dapat dimuatkan",
}
VIDEO_SIZE = {
    "zh": "約 {mb} MB，載入需要一點時間",
    "ja": "約 {mb} MB。読み込みに少し時間がかかります",
    "en": "About {mb} MB — it takes a moment to load",
    "ms": "Kira-kira {mb} MB — perlu masa sedikit untuk dimuatkan",
}
MODEL_TAG = {
    "zh": "模式輸出", "ja": "モデル計算", "en": "model output", "ms": "output model",
}


def media_card(item: dict, lang: str, *, is_video: bool = False) -> None:
    """STEM 用的影像／動畫卡：說明隨語言切換，來源仍與內容同框。

    產製者名稱與網址保持原文（專有名詞），只翻譯標籤與條款摘要——
    把 "NASA/SDO" 翻成中文反而讓人查不到出處。
    """
    meta = MEDIA.get(item.get("id", ""), {})
    title = meta.get("title", {}).get(lang) or item.get("title", "")
    note = meta.get("note", {}).get(lang) or ""

    st.markdown(f"**{title}**")
    st.caption(item.get("instrument", ""))

    if is_video:
        mb = item.get("approx_mb")
        if mb:
            st.caption(VIDEO_SIZE[lang].format(mb=mb))
        try:
            st.video(item["url"])
        except Exception:
            st.warning(LOAD_FAIL[lang])
    else:
        try:
            cadence = int(item.get("cadence_s") or 900)
            bucket = int(datetime.now(timezone.utc).timestamp() // cadence)
            sep = "&" if "?" in item["url"] else "?"
            st.image(f"{item['url']}{sep}_ts={bucket}", width='stretch')
        except Exception:
            st.warning(LOAD_FAIL[lang])

    if note:
        st.markdown(f"<div style='font-size:13px;line-height:1.6'>{note}</div>",
                    unsafe_allow_html=True)

    attr = item.get("attribution", {})
    st.caption(f"{SOURCE_LABEL[lang]}：{attr.get('provider', '')}　"
               f"[{attr.get('url', '')}]({attr.get('url', '')})")

def t(key: str, lang: str) -> str:
    return T.get(key, {}).get(lang, T.get(key, {}).get("en", key))


# ── 遊戲題庫 ────────────────────────────────────────────────────────────
G1_OPTIONS = {
    "zh": ["閃焰的光（X 射線）", "高能質子", "CME 電漿雲"],
    "ja": ["フレアの光（X線）", "高エネルギー陽子", "CME のプラズマ雲"],
    "en": ["The flare's light (X-rays)", "High-energy protons", "The CME plasma cloud"],
    "ms": ["Cahaya suar (sinar-X)", "Proton bertenaga tinggi", "Awan plasma CME"],
}
G1_EXPLAIN = {
    "zh": "光以光速前進，8 分 20 秒就到。質子要幾十分鐘到幾小時，CME 要 1～3 天。"
          "**這就是為什麼閃焰無法預警**——看到的同時它已經影響到高頻通訊了。",
    "ja": "光は光速で進むので8分20秒。陽子は数十分〜数時間、CME は1〜3日かかります。"
          "**だからフレアは予報できません**——見えた時にはもう短波通信に影響が出ています。",
    "en": "Light travels at light speed and takes 8 min 20 s. Protons take tens of minutes to hours; "
          "a CME takes 1–3 days. **This is why flares cannot be forecast** — by the time you see one, "
          "it is already disrupting radio.",
    "ms": "Cahaya bergerak pada laju cahaya dan mengambil 8 minit 20 saat. Proton mengambil puluhan minit "
          "hingga jam; CME mengambil 1–3 hari. **Inilah sebabnya suar tidak boleh diramal** — apabila anda "
          "melihatnya, ia sudah mengganggu radio.",
}

G2_YES = {"zh": "會有地磁暴", "ja": "磁気嵐になる", "en": "Storm likely", "ms": "Ribut berkemungkinan"}
G2_NO = {"zh": "應該不會", "ja": "たぶん起きない", "en": "Probably not", "ms": "Mungkin tidak"}


def _g2_case(rng: random.Random) -> tuple[float, float, bool]:
    """產生一組太陽風條件。Bz 南向且夠強才會有暴。"""
    bz = round(rng.uniform(-25.0, 15.0), 1)
    speed = round(rng.uniform(300.0, 850.0), 0)
    # 教學上的簡化規則：Bz < -8 且風速 > 450 才判定會有暴
    storm = bz < -8.0 and speed > 450.0
    return bz, speed, storm


def _g2_explain(bz: float, speed: float, storm: bool, lang: str) -> str:
    if lang == "ja":
        d = "南向き" if bz < 0 else "北向き"
        return (f"Bz = {bz} nT（{d}）、風速 {speed:.0f} km/s。"
                + ("南向きで強く、風速も速いのでエネルギーが流れ込みます。"
                   if storm else "南向きが弱いか風速が足りないので、磁気圏は持ちこたえます。"))
    if lang == "en":
        d = "southward" if bz < 0 else "northward"
        return (f"Bz = {bz} nT ({d}), speed {speed:.0f} km/s. "
                + ("Strongly southward and fast, so energy pours in."
                   if storm else "Not southward enough, or too slow — the magnetosphere holds."))
    if lang == "ms":
        d = "ke selatan" if bz < 0 else "ke utara"
        return (f"Bz = {bz} nT ({d}), laju {speed:.0f} km/s. "
                + ("Kuat ke selatan dan laju, jadi tenaga mencurah masuk."
                   if storm else "Tidak cukup ke selatan, atau terlalu perlahan — magnetosfera bertahan."))
    d = "朝南" if bz < 0 else "朝北"
    return (f"Bz = {bz} nT（{d}），風速 {speed:.0f} km/s。"
            + ("南向且夠強，風速也快，能量會大量灌進來。"
               if storm else "南向不夠強或風速不夠快，磁層擋得住。"))


def _g3_case(rng: random.Random) -> tuple[float, int]:
    kp = rng.choice([5.0, 5.7, 6.0, 6.3, 7.0, 7.7, 8.0, 8.3, 9.0])
    return kp, min(5, int(kp) - 4)


# ── 渲染 ────────────────────────────────────────────────────────────────
def _state(key: str, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


def render(store, registry_fn, image_card, images_by_id,
           animation_card=None, animations_by_id=None) -> None:
    """STEM 教學頁。

    參數以函式傳入而非 import，避免與 app.py 形成循環相依。
    """
    lang_label = st.selectbox("🌐 Language ／ 語言 ／ 言語 ／ Bahasa", list(LANGS), index=0)
    lang = LANGS[lang_label]

    st.title(t("title", lang))
    st.caption(t("subtitle", lang))
    st.divider()

    # ── 一、太陽 ──
    st.header(t("s1_head", lang))
    col_txt, col_img = st.columns([3, 2])
    with col_txt:
        st.markdown(t("s1_body", lang))
    with col_img:
        items = images_by_id("sdo_white_light")
        if items:
            media_card(items[0], lang)

    st.divider()

    # ── 二、速度 ──
    st.header(t("s2_head", lang))
    st.markdown(t("s2_body", lang))
    # 動畫比靜態圖更容易讓學生看懂「太陽是活的」與「CME 往哪裡噴」
    if animations_by_id:
        anims = animations_by_id("sdo_304_video", "soho_c2_video")
        for col, anim in zip(st.columns(max(1, len(anims))), anims):
            with col:
                media_card(anim, lang, is_video=True)
    items = images_by_id("soho_lasco_c2", "sdo_euv_304")
    for col, item in zip(st.columns(max(1, len(items))), items):
        with col:
            media_card(item, lang)

    st.divider()

    # ── 三、磁場 ──
    st.header(t("s3_head", lang))
    st.markdown(t("s3_body", lang))
    items = images_by_id("swpc_ovation_north")
    if items:
        media_card(items[0], lang)

    st.divider()

    # ── 四、影響 ──
    st.header(t("s4_head", lang))
    st.markdown(t("s4_body", lang))

    st.divider()

    # ── 五、即時資料 ──
    st.header(t("s5_head", lang))
    st.info(t("live_note", lang))

    LIVE = [
        ("ISN", {"zh": "太陽黑子數", "ja": "黒点数", "en": "Sunspot number", "ms": "Bilangan tompok"}, "{:.0f}"),
        ("F107_OBS", {"zh": "太陽電波強度", "ja": "太陽電波強度", "en": "Solar radio flux", "ms": "Fluks radio suria"}, "{:.0f} sfu"),
        ("KP_3H", {"zh": "地磁擾動 Kp", "ja": "地磁気擾乱 Kp", "en": "Geomagnetic Kp", "ms": "Kp geomagnet"}, "{:.2f}"),
        ("IMF_BZ", {"zh": "Bz（南北向）", "ja": "Bz（南北）", "en": "Bz (south/north)", "ms": "Bz (selatan/utara)"}, "{:+.1f} nT"),
        ("SW_V", {"zh": "太陽風速度", "ja": "太陽風速度", "en": "Solar wind speed", "ms": "Laju angin suria"}, "{:.0f} km/s"),
    ]
    now = datetime.now(timezone.utc)
    cols = st.columns(len(LIVE))
    missing = False
    for col, (code, label, fmt) in zip(cols, LIVE):
        df = store.query(code, start=now - timedelta(days=3), end=now)
        value = None if df.empty else float(df.sort_values("valid_time").iloc[-1]["value"])
        with col:
            if value is None:
                st.metric(label.get(lang, code), "—")
                missing = True
            else:
                st.metric(label.get(lang, code), fmt.format(value))
    if missing:
        st.caption(t("no_data", lang))

    st.divider()

    # ── 六、遊戲 ──
    st.header(t("games_head", lang))

    # 遊戲 1
    with st.container(border=True):
        st.subheader(t("g1_title", lang))
        st.markdown(t("g1_q", lang))
        opts = G1_OPTIONS[lang]
        pick = st.radio("", opts, index=None, key="stem_g1", label_visibility="collapsed")
        if st.button(t("check", lang), key="stem_g1_btn"):
            if pick == opts[0]:
                st.success(f"✅ {t('correct', lang)}")
            elif pick is None:
                st.warning("…")
            else:
                st.error(f"❌ {t('wrong', lang)}")
            st.info(G1_EXPLAIN[lang])

    # 遊戲 2
    with st.container(border=True):
        st.subheader(t("g2_title", lang))
        st.caption(t("g2_intro", lang))
        seed = _state("stem_g2_seed", 1)
        score = _state("stem_g2_score", [0, 0])
        bz, speed, storm = _g2_case(random.Random(seed))

        c1, c2 = st.columns(2)
        c1.metric("Bz", f"{bz:+.1f} nT")
        c2.metric("V", f"{speed:.0f} km/s")

        b1, b2, b3 = st.columns([1, 1, 1])
        answered = None
        if b1.button(G2_YES[lang], key="stem_g2_yes", width='stretch'):
            answered = True
        if b2.button(G2_NO[lang], key="stem_g2_no", width='stretch'):
            answered = False
        if answered is not None:
            score[1] += 1
            if answered == storm:
                score[0] += 1
                st.success(f"✅ {t('correct', lang)}")
            else:
                st.error(f"❌ {t('wrong', lang)}")
            st.info(_g2_explain(bz, speed, storm, lang))
        if b3.button(t("next", lang), key="stem_g2_next", width='stretch'):
            st.session_state["stem_g2_seed"] = seed + 1
            st.rerun()
        st.caption(f"{t('score', lang)}: {score[0]} / {score[1]}")

    # 遊戲 3
    with st.container(border=True):
        st.subheader(t("g3_title", lang))
        st.caption(t("g3_intro", lang))
        seed3 = _state("stem_g3_seed", 1)
        score3 = _state("stem_g3_score", [0, 0])
        kp, level = _g3_case(random.Random(seed3))

        st.metric("Kp", f"{kp:g}")
        cols3 = st.columns(5)
        chosen = None
        for i, col in enumerate(cols3, start=1):
            if col.button(f"G{i}", key=f"stem_g3_{i}", width='stretch'):
                chosen = i
        if chosen is not None:
            score3[1] += 1
            if chosen == level:
                score3[0] += 1
                st.success(f"✅ {t('correct', lang)}　Kp {kp:g} → G{level}")
            else:
                st.error(f"❌ {t('wrong', lang)}　Kp {kp:g} → G{level}")
        if st.button(t("next", lang), key="stem_g3_next"):
            st.session_state["stem_g3_seed"] = seed3 + 1
            st.rerun()
        st.caption(f"{t('score', lang)}: {score3[0]} / {score3[1]}")

    if st.button(t("reset", lang), key="stem_reset"):
        for k in ("stem_g2_seed", "stem_g2_score", "stem_g3_seed", "stem_g3_score"):
            st.session_state.pop(k, None)
        st.rerun()

    st.divider()
    st.subheader(t("more_head", lang))
    st.markdown(t("more_body", lang))
