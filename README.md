# Nasdaq naujienos el. paštu

Atskira naujienų programa: atrenka viešas pasaulio naujienas, galinčias būti svarbias Nasdaq, ir prie antraštės bei šaltinio nuorodos prideda trumpą lietuvišką ekonominio ryšio paaiškinimą. Paaiškinimai parenkami pagal temą ir antraštės raktažodžius. Kainos krypties vertinimo ar AI API nėra.

Ši saugykla ir jos darbo eiga atskirtos nuo esamos NQ signalų saugyklos. Senos NQ saugyklos failai ir nustatymai nekeičiami. monitor.py ir volume_profile.py yra nepakeistos pagalbinės kopijos, naudojamos tik pašto siuntimo funkcijoms importuoti; NQ rinkos stebėjimas čia nepaleidžiamas.

## Būsena ir įjungimas

Be pašto nustatymų automatinė eiga tik perskaito viešus RSS kanalus ir rodo patikros rezultatą. Laiškų nesiunčia ir istorijos nesaugo. Kodo įkėlimas taip pat atlieka tik patikrą be laiškų. Pašto nustatymai į atskirą saugyklą iš seno projekto nepersikelia.

Saugyklos Settings → Secrets and variables → Actions pridėk tris repository secrets:

- GMAIL_USER – laiškus siunčiančios Gmail paskyros adresas.
- GMAIL_APP_PASSWORD – tos paskyros programos slaptažodis. Jo į pokalbį ar kodo failus nerašyk.
- ALERT_EMAIL – tas pats gavėjo adresas, kuriuo nori gauti naujienas.

Visi trys privalomi šios darbo eigos siuntimui. Tikri slaptažodžiai kode nesaugomi. Pridėjus nustatymus paleisk Actions → Nasdaq naujienos el. pastu → Run workflow. Pažymėjus Siusti viena bandomaji naujienu laiska siunčiamas vienas aiškiai pažymėtas testas; įprastas paleidimas atliekamas išjungus abi bandymo parinktis. Pirmas įprastas sėkmingas paleidimas įsimena esamas naujienas jų nesiųsdamas ir išsiunčia vieną parengties patvirtinimą.

## Kas siunčiama

Atskiri [NASDAQ ŽINIOS] laiškai, po vieną kiekvienai atrinktai naujai istorijai: šaltinio antraštė ir trumpa ištrauka originalo kalba, paskelbimo ir aptikimo laikas Lietuvoje, nuoroda ir „Ką tai gali reikšti Nasdaq“ paaiškinimas lietuviškai. Programa neverčia visų antraščių ir neatlieka viso straipsnio individualios analizės.

Tikrinimas numatytas visą parą kas 30 min., 7 ir 37 minutę kiekvieną valandą UTC; paleidimas bei šaltinio paskelbimas gali vėluoti. Naudojami 8 vieši RSS šaltiniai. Naujienos senumo riba 3 val.; per patikrą iki 6 laiškų, kiti lieka eilėje iki galiojimo pabaigos. Pasikartojimai slopinami, tačiau kitaip parašyta tos pačios istorijos versija kartais gali būti atskiras laiškas. Teminis filtras negarantuoja visų reikšmingų naujienų aptikimo.

Būsena ir laukiančių laiškų eilė laikoma tik šios saugyklos news_state/state.json. Po neaiškaus SMTP ryšio nutrūkimo retais atvejais galimas pakartojimas. Jei naudojama ta pati Gmail paskyra kaip NQ signalams, pašto siuntimo limitai vis dar bendri.

Saugykla vieša: kodas, pakeitimų istorija, GitHub Actions žurnalai ir news_state naujienų įrašai matomi visiems. Slaptos pašto nustatymų reikšmės laikomos GitHub Secrets ir viešai nerodomos. Standartiniai GitHub talpinami vykdytojai viešoms saugykloms nemokami. Tikrinimas numatytas kas 30 min. (cron: `7,37 * * * *`), o vykdymo riba – 2 min. Papildomos mokamos AI, naujienų ar el. pašto paslaugos neprijungtos.

## Vietinė patikra

Python 3.12: python -m pip install -r requirements.txt; python -m unittest discover -s tests -v; python news_monitor.py --dry-run. Tikram vietiniam siuntimui reikia aukščiau nurodytų aplinkos kintamųjų. Šioje atskiroje saugykloje yra 24 naujienų testai; paruoštas kodas taip pat patikrintas kartu su 70 nepakeistos NQ programos testų.
