# Leads-SE Router — startkontext för ny session

Skriven 2026-09-04. Komplement till `PROJECT.md`, som täcker domänen (Slack-meddelandeformat,
HubSpot-schema, routingregler). **Läs PROJECT.md för affärslogiken** — den upprepas inte här.
Det här dokumentet täcker infrastruktur, icke-uppenbara beslut och kända fel.

---

## 1. Syfte

Botten läser Slack-kanalen `#leads-se`, plockar upp demo-requests och FoAT-intresseanmälningar
som Zapier postar dit, slår upp kommunen (eller skolan) i HubSpot Companies och svarar i tråden
med en @-mention av rätt person: CSM om det är en befintlig kund, annars företagsägaren, annars
`@sales-swe` / `@cs_swe` som fallback.

Målet är att leads inte ska ligga oplockade för att ingen vet vems de är.

---

## 2. Arkitektur

Allt ligger i **en fil**: `router.py`. Inget ramverk, bara `requests` + `python-dotenv`.

```
Zapier → Slack #leads-se (bot B01J5UBRSUE)
              ↓  polling var 60:e sekund
         router.py på Railway
              ↓  slår upp namn i HubSpot Companies
         svar i tråden med @-mention
```

**Flödet i `run_once()`:** läs `last_seen.txt` → hämta meddelanden nyare än så → filtrera på
`bot_id == B01J5UBRSUE` → hoppa över trådar botten redan svarat i → parsa efter header →
`build_reply()` → posta trådsvar → spara ny tidsstämpel.

**Ägarupplösning, ordningen spelar roll:** `csm`-propertyn först, `hubspot_owner_id` först
därefter. `csm` är den som gäller för befintliga kunder — det är inte uppenbart av fältnamnen.

**Uppstart laddar tre uppslagstabeller en gång** (~102 HubSpot-ägare, ~188 Slack-användare):
e-post → Slack-ID, e-postprefix → Slack-ID, normaliserat namn → Slack-ID. Prefixmatchningen
finns för att HubSpot har `@magma.se` och Slack `@magmamath.com` för samma personer.

**Deploy-topologi:** Railway, projekt `leads-se-router`, miljö `production`, US West, 1 replica,
`worker: python -u router.py` via Procfile. Ingen exponerad port, ingen publik domän.

---

## 3. Beslut och lösningar som inte syns i koden

### Railway deployas via CLI, inte GitHub — detta är den viktigaste punkten

Hela deployment-historiken säger **"railway up … via CLI"**. Repot är **inte** kopplat till
Railway. En `git push` deployar därför **ingenting**. Det är lätt att tro motsatsen och sitta
och vänta på en deploy som aldrig kommer.

Och `railway`-CLI:t finns **inte installerat** på Annas maskin (verifierat 2026-09-04: inget
`railway` i PATH, inget `npm`/`node`/`scoop`/`choco` att installera med, Railway finns inte i
`winget`). Dashboarden är enda vägen just nu.

### Railways Healthcheck Path är en deploy-grind, inte en liveness-probe

Railways egen text: *"Endpoint to be called before a deploy completes."* Den anropas en gång
vid deploy, aldrig igen. **En HTTP-hälsoendpoint kan därför aldrig få en hängd worker omstartad.**

Det Railway faktiskt reagerar på är processavslut, via Restart Policy. Därför avslutar
watchdogen processen med `os._exit(1)` istället för att svara 503 till någon som aldrig frågar.
`os._exit` och inte `sys.exit` — det senare avvecklar bara tråden när det anropas från en
icke-huvudtråd.

### Två separata klockor, och varför

Restart Policy står på **On Failure med tak på 10 försök**. Överskrids taket slutar Railway
starta om — permanent, tills någon deployar om. Omstarter är alltså en begränsad resurs.

Därav:

| Klocka | Betyder | Åtgärd |
|---|---|---|
| `_last_tick` | loopen snurrar | stale >900 s → `os._exit(1)` → omstart |
| `_last_ok` | pollningen *lyckades* | stale >300 s → 503 på `/`, **ingen** omstart |

Ett Slack-avbrott får inte trigga omstarter: loopen lever, en omstart fixar ingenting, och tio
onödiga omstarter skulle döda tjänsten på riktigt. Bara en äkta hängning avslutar processen.

### `startup(retry)` har olika beteende i de två körlägena

`retry=True` i pollningsläge (försök igen var 30:e sekund istället för att dö),
`retry=False` med `--once`. Det senare är avsiktligt: `run_router.bat` kör `--once` från
Windows Task Scheduler, och ett schemalagt jobb får inte snurra i en oändlig retry-loop.

### Timeouts på 5/20 sekunder

`requests` har **ingen** default-timeout. Utan explicit timeout blockerar ett stallat anrop
loopen för alltid — en hängning som inget `except` kan fånga. Alla sju anropsställen har
`timeout=HTTP_TIMEOUT`.

### `python -u` i Procfile

Blockbuffrad stdout gjorde att en levande worker såg död ut i Railway-loggen. Utan `-u`
felsöker man i blindo.

### Svenska företag prioriteras på namnprefix

HubSpot innehåller företag från flera länder. Svenska heter `SWE - <NAMN>`, och
`hs_search_company` sorterar dem först. Enkelt, men bara begripligt om man vet namnkonventionen.

### Git push kräver gh-helpern

HTTPS-remoten har inga cachade credentials och git kan inte prompta i den här miljön:

```bash
git -c credential.helper="!gh auth git-credential" push origin main
```

`gh` är inloggad som `Code-Bjarkan` med `repo`-scope. `gh auth setup-git` gör det permanent
om man vill slippa flaggan.

---

## 4. Kända problem

### BLOCKER: senaste commiten är inte deployad

`50633f9` ("Harden router against hangs and boot-time restart loops") ligger på GitHub `main`
men **kör inte i produktion**. Aktiv deployment är `6af68a5e` från **2026-06-18** — junikod.

Fixa genom att koppla repot: **Settings → Source → Connect Repo** → `Code-Bjarkan/Leads-se`,
gren `main`. Alternativt installera Railway-CLI:t manuellt och köra `railway up`.

**Verifiering att det lyckats:** den nya loggen ska innehålla raden
`Watchdog armed (exit after 900s without a loop tick).` vid uppstart. Saknas den kör du fortfarande junikoden.

### FIXAD 2026-09-04: `last_seen` trunkerades, samma meddelande behandlades om varje minut

`save_last_seen` gjorde `int(float(ts))` och kastade decimalerna. Slack-ts `1785956216.196599`
sparades som `1785956216`, och strängjämförelsen i `run_once` läste den längre strängen som
större — meddelandet filtrerades aldrig bort. Syntes i produktionsloggen som
`Already replied — skipping.` för samma ts, var 60:e sekund, i timmar. Cirka **1440 bortkastade
Slack-anrop per dygn per fastnat meddelande**.

Nu sparas hela tidsstämpelsträngen. En gammal trunkerad fil självläker: meddelandet kommer
tillbaka en gång, idempotenskontrollen hoppar över det, och full tidsstämpel skrivs.
En korrupt fil faller tillbaka på entimmarsfönstret istället för att krascha.

### FIXAD 2026-09-04: `.co.uk` gav skräpkandidaten `Co`

`email_domain_candidates("…@magmamaths.co.uk")` delade domänen i `['magmamaths','co','uk']`,
släppte bara sista labeln och lämnade `co` som sökterm. I produktion 2026-08-07 08:55 matchade
det ett skoldistrikt i **Colorado** och taggade fel person:

```
[fallback] Matched 'CO-SD AURORA JOINT DISTRICT NO. 28 OF THE COUNTIES OF ADAMS AND A'
```

Nu finns `_MULTIPART_TLDS` (co.uk, com.au, org.uk m.fl.) som släpper båda labelerna, plus
`_MIN_LABEL_LEN = 3` som backstop — ingen svensk kommun har ett namn kortare än tre tecken.
Listan täcker de vanligaste suffixen men är inte uttömmande; lägg till fler när de dyker upp.

### Medvetet nedprioriterat

- **`last_seen.txt` på efemer disk.** Vid omstart faller den tillbaka på en timme bakåt och
  skannar om. Idempotenskontrollen hindrar dubbelposter. Vid Annas volym (några leads per dag)
  är kostnaden försumbar. Fix vore en Railway-volume.
- **Ingen paginering.** `slack_get_messages` hämtar `limit: 50` men flyttar tidsstämpeln till
  nyaste meddelandet, så fler än 50 meddelanden mellan två pollningar tappas tyst. Kräver 50
  meddelanden på 60 sekunder — orealistiskt här. Notera dock att gränsen räknar *alla*
  kanalmeddelanden, inte bara botens.

### PROJECT.md är delvis inaktuell

- Avsnitt 3 säger att lead/opportunity ska taggas med *"owner of the most recently active deal"*.
  Koden gör inte det — den använder `hubspot_owner_id` på Company. Deals rörs aldrig.
- Avsnitt 3 säger `#cs-sweden` (kanal); koden taggar användargruppen `@cs_swe` (`S09LZN0TMLL`).
- Avsnitt 5/6 speglar bara majbygget.

### Ursprungsdiagnosen som inte höll

Sessionen började med antagandet att containern gick ner återkommande. Loggarna visar motsatsen:
deployment `6af68a5e` har varit aktiv sedan 18 juni — **~50 dagar** — och tickar var 60:e sekund
utan avbrott, tracebacks eller omstarter i det granskade fönstret.

Hypoteserna om OOM, minnesläcka, Serverless-scale-to-zero och uttömd restart-budget är alla
**utan stöd i data**. Timeouts och watchdog är rimliga skydd, men de löste inget pågående problem.
Vill man täcka hela 50-dagarsperioden: sök på `Loaded` i loggfiltret — varje träff är en processtart.

---

## 5. Nästa steg, i ordning

1. **Koppla Railway till GitHub-repot** och deploya. Inget annat spelar roll förrän detta är
   gjort — produktion kör fortfarande junikod, och alla fixar nedan ligger odeployade.
2. Verifiera i den nya loggen: raden `Watchdog armed (exit after 900s…)` ska finnas, och
   `Already replied — skipping.` ska **sluta** upprepas varje minut.
3. Valfritt: höj restart-taket 10 → 20 i Settings → Deploy.
4. Valfritt: committa `PROJECT.md`, `smoke_test.py`, `run_router.bat` — de är otrackade men
   hör hemma i repot. `router.log`, `router_err.log`, `last_seen.txt` och `debug_*.py` bör
   däremot inte in.

---

## 6. Kommandon

**Kör en gång lokalt** (postar på riktigt i Slack — `SUGGEST_ONLY = False` i koden):

```bash
cd "C:\Users\anna\OneDrive\Desktop\Hubspot" && .venv\Scripts\python.exe router.py --once
```

**Kör pollningsloopen lokalt** (startar även hälsoendpoint på `:8080` och watchdog):

```bash
cd "C:\Users\anna\OneDrive\Desktop\Hubspot" && .venv\Scripts\python.exe -u router.py
```

**Hälsoendpoint:**

```bash
curl http://localhost:8080/
```

Svarar `200 ok last_success=Ns ago last_tick=Ns ago`, eller `503 stale …`.

**Justerbart via env-variabler:** `HEALTH_MAX_STALE_SECONDS` (default 300),
`WATCHDOG_MAX_STALE_SECONDS` (default 900), `PORT` (default 8080).

**Push:**

```bash
git -c credential.helper="!gh auth git-credential" push origin main
```

**Loggar och drift:** Railway-dashboarden. Deployments → View Logs. Sökfältet är mer användbart
än scrollning — sök `Loaded` (processtarter), `Traceback` (krascher), `Error:` (fångade fel),
`Watchdog` (bekräftar ny kod).

**Det finns inga automatiska tester.** Verifieringen i den här sessionen gjordes med
engångsskript i scratchpad-katalogen. `smoke_test.py` i repot är ett fristående HubSpot-uppslag
som gör riktiga API-anrop, inte ett test av `router.py`.

---

## 7. Miljö

`.env` lokalt (gitignorerad) och Railway-variabler innehåller `HUBSPOT_TOKEN`,
`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`. Sanningskällan är `.env` på Annas maskin.
HubSpot-token har `pat-eu1-`-prefix (EU-instans; standardhosten `api.hubapi.com` fungerar ändå).

Railway-inställningar per 2026-09-04: Restart Policy **On Failure / 10**, Serverless **av**,
Healthcheck Path **osatt** (lämna den så — se avsnitt 3), ingen publik domän, bara
`leads-se-router.railway.internal`.

GCP-projektet `leads-se-router-magma` finns men saknar fakturering. `cloud_function/` är ett
händelsestyrt alternativ som aldrig deployats.

Windows Task Scheduler-jobbet `\LeadsSERouter` är avstängt. Railway är enda körningen.
