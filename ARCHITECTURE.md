# Athos Contabile — Architettura

Bot Telegram contabile per **Buongiorno Egitto** (agenzia viaggi in Egitto).
Trasforma messaggi liberi (`+200 tour piramidi`) e comandi slash (`/raccolgo`,
`/verso`, `/cambia`, `/paga_fornitore`) in scritture contabili a partita
doppia salvate su Supabase. La dashboard `thoth-dashboard` (Streamlit, repo
separato) legge da Supabase per mostrare saldi, report e P&L.

Documento aggiornato al **2026-05-10** (refactor parser Claude → tool use).
Quando il bot cambia in modo significativo, aggiornare questo file PRIMA di
committare.

---

## 1. Big picture

```
┌──────────────┐     messaggi liberi          ┌─────────────────┐
│   Telegram   │ ───────────────────────────► │     Athos       │
│  (guide,     │ ◄─────  conferme  ─────────  │   bot.py        │
│   Amr, Omar) │     /raccolgo /verso ecc.    │  (Railway)      │
└──────────────┘                              └────────┬────────┘
                                                       │
                              parse libero             │ slash command
                              (1 chiamata)             │
                                       ▼               ▼
                                   ┌────────┐     ┌──────────────┐
                                   │ Claude │     │  Logica      │
                                   │  API   │     │ deterministica│
                                   └───┬────┘     └──────┬───────┘
                                       │                 │
                                       └────────┬────────┘
                                                ▼
                            ┌───────────────────────────────────────┐
                            │  insert_journal_entry()               │
                            │  - INSERT journal_entries (header)    │
                            │  - INSERT journal_lines (dare/avere)  │
                            │  - RPC check_entry_balanced()  ◄ if   │
                            │    sbilanciato → rollback             │
                            └────────────────┬──────────────────────┘
                                             │
                              ┌──────────────┴───────────────┐
                              ▼                              ▼
                       ┌─────────────┐               ┌────────────────┐
                       │  Supabase   │               │  Google Sheet  │
                       │  Postgres   │               │   (legacy,     │
                       │ (canonical) │               │   solo guide)  │
                       └──────┬──────┘               └────────────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │  thoth-dashboard     │
                   │  (Streamlit, leggi)  │
                   └──────────────────────┘
```

**Tre ruoli funzionali**:

- **Guida** (Saif, Abozeidm, Maja, …) — scrive messaggi liberi tipo
  `+200 tour piramidi`, `-50 LE acqua clienti`. Il bot li parsa via Claude
  e crea la scrittura sulla cassa della guida.
- **Contabile** (Amr) — usa `/raccolgo` per incassare cassa dalle guide,
  `/verso` per consegnarla a Omar o alla banca, `/cambia` per cambiare EUR
  → EGP, `/paga_fornitore` per pagare Shamandura/Shaarawy/altri.
- **Proprieta** (Omar) — può fare TUTTO quello che fa il contabile, più
  scrivere messaggi liberi come fosse una guida (i soldi vanno sul conto
  `proprieta`). Riceve il report cassa giornaliero alle 20:00 ora del Cairo.

**Manager**: alias di `guida` con label diversa (vedi migrazione 024). Stesso
comportamento del ruolo `guida` lato bot.

---

## 2. Stack tecnico

| Layer        | Tecnologia                           | Note                                    |
|--------------|--------------------------------------|-----------------------------------------|
| Bot          | Python 3.11 + `python-telegram-bot` 20.7 | un singolo file `bot.py` (~2880 righe) |
| Hosting      | Railway (auto-deploy da GitHub)      | branch `BuongiornoEgitto-patch-1`       |
| AI parsing   | Claude API (Anthropic)               | `claude-sonnet-4-6`, prompt caching ON  |
| DB           | Supabase Postgres (REST API + RPC)   | progetto `kwcznnsddtwhnfqtinvk`         |
| Backup legacy| Google Apps Script + Google Sheet    | dual-write SOLO eventi economici reali  |
| Dashboard    | Streamlit (`thoth-dashboard/`)       | deploy separato, legge da Supabase      |

**Dipendenze runtime** (requirements.txt):
```
python-telegram-bot[job-queue]==20.7
requests==2.31.0
```

**Dockerfile**: base `python:3.11-slim`, copia `bot.py` + requirements, lancia
`python bot.py`. Niente di più.

---

## 3. Schema database (essenziale)

Tutto il dettaglio è nei file `thoth-dashboard/db/migrations/*.sql`. Sintesi
dei concetti che il bot usa:

### `accounts` — piano dei conti
Definito staticamente nella migrazione 001, modificato dalle successive.
Tipi: `attivo` | `ricavo` | `costo` | `passivo` | `patrimonio`.

Conti chiave per il bot:
- **Casse fisiche** (`attivo`): `cassa_guida_<nome>`, `cassa_contabile`,
  `proprieta`, `banca`. Sono i conti dove "stanno fisicamente i soldi".
- **Cambio valuta** (`attivo`): `cambio_valuta` — conto di transito per `/cambia`.
- **Casse fornitori** (`attivo`): `cassa_fornitore_<nome>` — depositi versati
  ai fornitori, consumati man mano (vedi `/paga_fornitore`).
- **Ricavi/Costi**: `ricavi_escursioni` (default), `ricavi_commissioni`,
  `costi_escursioni` (consolidato, vedi 022), `costi_ristoranti`, ecc.

### `telegram_users` — mapping Telegram → conto
```
telegram_user_id (PK) | display_name | username | role | account_code | …
```
- Auto-upsertato a ogni messaggio (`upsert_telegram_user`).
- Ruolo e `account_code` partono NULL → Omar li mappa a mano dal Supabase
  Table Editor o dalla pagina admin della dashboard.
- Senza `account_code` un utente può scrivere ma NON può registrare nulla
  (riceve "⏳ Ti ho registrato ma Omar deve ancora associarti a un conto").

### `journal_entries` — header di una scrittura
Una entry = un evento economico ("Saif ha incassato 200 da un tour"). Ha
descrizione, data, source (`telegram` / `manual` / `checkfront` / `import`),
ID dell'utente Telegram autore.

### `journal_lines` — righe dare/avere
Ogni riga ha **dare XOR avere** (constraint a livello tabella) e una
**currency** (`EUR` o `EGP`). La somma `dare = avere` deve valere **per
ogni currency separatamente** (controllo via RPC `check_entry_balanced`,
chiamato dal bot dopo gli INSERT — se fallisce, il bot fa rollback
cancellando l'entry).

### Viste utili
- `cash_position` — saldi delle casse fisiche per currency.
- `account_balances` — saldo running per account.
- `unbalanced_entries` — sanity check, dovrebbe SEMPRE essere vuota.
- `v_transactions` (mig. 002 + 014) — view "stile single-entry" che la
  dashboard usa per visualizzare le transazioni.

### Audit log
Migrazioni 020/021/025: ogni INSERT/UPDATE/DELETE su tabelle critiche è
loggato in `audit_log` (immutabile, tipica forensic chain). Da consultare
in caso di sospetto errore o per ricostruire chi ha fatto cosa.

---

## 4. Flussi principali

### 4.1 Messaggio libero (la "killer feature")

Una guida scrive `+200 tour piramidi`. Il bot:

1. **`handle_message`** intercetta il messaggio. Filtra:
   - Group chat: blocca se non è il primo gruppo visto (anti-spam).
   - Messaggi che iniziano con `@` o contengono mention: chatter, ignora.
   - Reply a un altro umano (non al bot): chatter, ignora.
2. **`upsert_telegram_user`** registra/aggiorna l'utente.
3. Se l'utente non ha `account_code` → "⏳ Omar deve associarti a un conto".
4. **Pending preview check**: se c'è un preview attivo (multi-tx o
   importo grosso), tenta di interpretare il messaggio come risposta
   (`ok` / `no` / `solo 1,3,5`). Vedi `_parse_confirmation`.
5. **Detect multi-transazione**: regex `_DETECT_PATTERN` cerca pattern
   tipo `+N` / `-N` / `entrata N` / `uscita N` / `100 LE`. Se ne trova
   2+ → split via `_split_transactions` e parsing parallelo (1 chiamata
   Claude per ogni pezzo).
6. **`ask_claude(testo)`** → Claude usa il tool `register_transaction`
   (output strutturato, schema rigoroso). Ritorna `("tx", dict)` con
   campi tipati `{tipo, currency, importo, descrizione, account_code,
   confidence}` se il messaggio è una transazione, oppure `("msg", str)`
   con un testo da mostrare all'utente (non-transazione o errore API).
   L'enum di `account_code` è caricato da Supabase al boot
   (`fetch_active_economic_accounts`) → Claude non può inventare codici
   che non esistono.
7. **Sospetto?** Per **singola transazione** chiamiamo `_is_high_amount`
   (solo soglie €1900 / 60k LE — vedi 4.7 sotto per il razionale). Se
   sospetto → preview "Ho trovato 1 transazione. Registro?". Per
   **multi-transazione** la preview viene mostrata sempre, e ogni riga
   è marcata ⚠️/✅ in base a `_is_suspect` (più strict: include anche
   `confidence=low` e fallback account + descrizione corta).
8. **`_write_one_transaction`** → costruisce le righe dare/avere via
   `_build_economic_lines` e chiama `insert_journal_entry`:
   - Entrata: dare `cassa_dell_autore`, avere `account_code` (es. `ricavi_escursioni`).
   - Uscita: dare `account_code` (es. `costi_ristoranti`), avere `cassa_dell_autore`.
9. **Dual-write Google Sheet** (legacy). SOLO per eventi economici reali
   (entrate/uscite con conto economico). I trasferimenti `/raccolgo`,
   `/verso`, `/cambia`, `/paga_fornitore` NON vanno sullo Sheet.
10. Risposta finale: "✅ Registrato +200 EUR — tour piramidi → ricavi_escursioni".

### 4.2 `/raccolgo` — il contabile (o chiunque) raccoglie cassa da un altro

Aperto a chiunque abbia un `account_code` (vedi `_require_account_user`).
Il conto **destinatario** è l'`account_code` di chi scrive il comando; il
**mittente** è scelto via tastiera o passato come argomento.

Modalità:
```
/raccolgo                  → chiede importo, poi mostra tastiera utenti
/raccolgo 200              → 200 EUR, mostra tastiera
/raccolgo 5000le           → 5000 EGP, mostra tastiera
/raccolgo 200 saif         → 200 EUR diretto da Saif
/raccolgo 5000 le saif     → 5000 EGP diretto da Saif
/raccolgo 5000lire saif    → 5000 EGP diretto da Saif
```

Scrittura:
```
dare  <conto di chi raccoglie>  importo
avere <conto di chi consegna>   importo
```

### 4.3 `/verso` — versa cassa a un altro o alla banca

Stesso pattern ma inverso: chi scrive è il mittente, sceglie un destinatario
(altro utente o `banca` o `omar`/`proprieta` come alias).

Modalità identiche a `/raccolgo` con destinazione invece di "da chi". La
tastiera include un bottone `🏦 Banca` extra.

Scrittura:
```
dare  <destinazione>             importo
avere <conto di chi versa>       importo
```

### 4.4 `/cambia` — cambio valuta EUR → EGP

Riservato a contabile/proprieta. Scrittura **a 4 righe** (2 EUR + 2 EGP)
bilanciata per currency, usa `cambio_valuta` come conto di transito:
```
dare  cambio_valuta      EUR <eur>
avere <cassa_autore>     EUR <eur>     ← perde EUR dalla tasca
dare  <cassa_autore>     EGP <egp>     ← riceve EGP in tasca
avere cambio_valuta      EGP <egp>
```

Modalità:
```
/cambia                      → flow: chiede EUR, poi EGP, poi conferma
/cambia 100                  → flow accorciato: chiede solo EGP
/cambia 100 5000             → mostra subito conferma (rate calcolato)
```

Il bot calcola il rate, mostra il riepilogo con tastiera Conferma/Annulla,
poi scrive l'entry SOLO dopo il click.

### 4.5 `/paga_fornitore` — paga un fornitore (Shamandura, Shaarawy, …)

Riservato a contabile/proprieta. Due modalità diverse a seconda di chi mette
i soldi:

**A. Pagamento dalla cassa dell'agenzia** (caso normale).
Pattern a 4 righe (vedi commit 99f531a): il pagamento passa per
`cassa_fornitore_<nome>` (deposito), che poi viene "consumato" come
`costi_escursioni`. Permette di tenere traccia del credito verso il
fornitore separatamente dal costo già rilevato.

**B. Pagamento del cliente direttamente al fornitore** (aggiunto 2026-05-10).
Quando il turista paga in contanti il fornitore sul posto (es. dà 100 EUR a
Shamandura in barca), l'agenzia non muove cassa propria. Salviamo solo un
`journal_entries` header (zero `journal_lines`) con:
- `source = 'cliente_paga_fornitore'`
- `customer_name = 'Mario Rossi'`
- `description` con fornitore, cliente, importo

`cash_position` e P&L invariati. La entry resta visibile per audit/statistiche
(query: `SELECT … FROM journal_entries WHERE source='cliente_paga_fornitore'`).

Flow conversazionale (5 step):
1. scegli fornitore
2. importo
3. fonte: cassa interna **oppure** "🧑 Cliente (paga direttamente)"
4. (solo se Cliente) → scrivi nome e cognome del cliente
5. conferma → scrittura

Vedi migrazione `026_customer_paid_supplier.sql` per le modifiche allo schema
(estensione CHECK su source + colonna `customer_name`).

### 4.6 `/report_cassa` (e job giornaliero alle 20:00)

Riservato a contabile/proprieta. Calcola lo snapshot della `cassa_contabile`
del giorno corrente: saldo iniziale + movimenti del giorno + saldo finale,
diviso per EUR/EGP. Usato sia on-demand (`/report_cassa`) sia automaticamente
ogni sera alle 20:00 ora del Cairo via `application.job_queue` (vedi
`send_daily_cash_report`). Il report viene mandato in DM all'utente con
ruolo `proprieta` (Omar).

### 4.7 Logica "sospetto" / preview

Due funzioni distinte (lo split è recente, vedi commit `4191deb`):

- **`_is_high_amount(tx)`** — solo soglia importo (`> €1900` / `> 60k LE`).
  Usata per decidere se la **singola transazione** passa per preview.
- **`_is_suspect(tx)`** — più strict: include anche `confidence=="low"` da
  Claude (campo dell'output strutturato del tool, sostituisce il vecchio
  `needs_review`) e account fallback (`costi_altri`/`ricavi_escursioni`)
  con descrizione cortissima. Usata SOLO per marcare con ⚠️ le righe nel
  preview multi-transazione.

**Razionale**: Omar (2026-05-08) ha chiesto zero friction sulle singole
transazioni quando la struttura del messaggio è chiara. Le incertezze di
classificazione di Claude (es. `+1 acqua`) non bloccano più. Le grosse cifre
e i messaggi multi-tx sì.

---

## 5. Codice — guida al `bot.py`

Il file è organizzato in sezioni separate da banner `# =======`. Indicizzato
qui per riferimento veloce.

| Riga    | Sezione                                                |
|---------|--------------------------------------------------------|
| 1-16    | Module docstring (riassunto operativo)                 |
| 41-49   | Config — env vars (`TELEGRAM_TOKEN`, `ANTHROPIC_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SHEETS_URL`) |
| 51-57   | Costanti API Anthropic (`ANTHROPIC_MESSAGES_URL`, `ANTHROPIC_MODEL`, timeouts) |
| 59-93   | `validate_environment()` — fail-fast su env mancanti  |
| 95-108  | Stato preview multi-tx (`_pending_previews`) e soglie sospetto |
| 110-200 | `SYSTEM_PROMPT` per Claude — istruzioni su quando chiamare il tool, regole tipo/currency/account_code/descrizione/confidence + esempi |
| 200-360 | Helpers Supabase REST (`upsert_telegram_user`, `get_telegram_user`, `find_user_by_*`, `fetch_users_with_account`, `fetch_active_economic_accounts`) |
| 380-490 | Journal entry write/rollback + dual-write Sheet       |
| 495-620 | Tool schema `register_transaction` (`_build_register_transaction_tool`, `init_claude_tool`) e `ask_claude` con tool use |
| 715-755 | `_build_economic_lines` (entrata/uscita, una currency per tx) |
| 569-720 | Multi-transaction parse, preview format, suspect heuristics |
| 745-810 | Write singolo + format conferma                        |
| 813-860 | Parse della risposta utente al preview (`ok` / `no` / `solo 1,2,3`) |
| 864-1100| **`handle_message`** — il routing principale dei messaggi liberi |
| 1124-1170| `_require_admin` / `_require_account_user`            |
| 1175-1247| Helpers EUR/EGP per slash commands                    |
| 1250-1495| `/raccolgo` — `_do_raccolgo`, tastiera, callback      |
| 1499-1718| `/verso` — `_do_verso`, tastiera, callback            |
| 1719-1972| `/cambia` — flow EUR→EGP a 4 righe                    |
| 1976-2275| `/paga_fornitore` — flow 4 righe via cassa_fornitore  |
| 2277-2543| Report cassa giornaliero (snapshot + job 20:00 Cairo) |
| 2545-2562| `/whoami` — debug del proprio mapping                 |
| 2564-2655| `/start` — istruzioni per ruolo                       |
| 2678-2700| `GUIDA_COMMANDS` / `ADMIN_COMMANDS` (autocomplete Telegram) |
| 2719-2766| `_on_startup` — registra i comandi su Telegram per scope |
| 2766-2882| `main()` — ConversationHandler setup, run polling     |

### Pattern usati

**Conversation flows (multi-step)**: ogni slash che fa più di una domanda
usa `ConversationHandler` di python-telegram-bot. Stati definiti come
costanti intere (`RACC_AMOUNT = 10`, `VERSO_AMOUNT = 11`, ecc.). Fallback
sempre `/annulla`.

**Inline keyboard callback**: formato `callback_data` standardizzato:
- Selezione utente per `/raccolgo` o `/verso`:
  `<prefix>:<currency>:<importo>:<account_code>` (es. `racc:EUR:200.00:cassa_guida_saif`).
- Cancel: `<prefix>_cancel`.
- `/cambia` confirm: `cambia_confirm` / `cambia_cancel`.

Limite Telegram: 64 byte per `callback_data`. I formati sopra stanno
comodamente dentro.

**Idempotency telegram_users**: l'upsert usa `on_conflict=telegram_user_id`
con `Prefer: resolution=merge-duplicates`, così ogni messaggio aggiorna
display_name/last_seen senza duplicare la riga.

**Bilanciamento e rollback**: `insert_journal_entry` fa 3 chiamate REST
(INSERT entries, INSERT lines, RPC check). Se il check fallisce, chiama
`_rollback_entry` che cancella l'entry (le lines vanno via via cascade).
**Limite noto**: tra il primo e il terzo passo non c'è una transazione
DB vera (siamo su REST), quindi una entry sbilanciata può esistere per
qualche ms se il processo viene killato in mezzo. La view
`unbalanced_entries` esiste apposta per pescarle.

**Prompt caching**: `ask_claude` mette il `SYSTEM_PROMPT` con
`cache_control: ephemeral` → dopo la prima chiamata, i 5 minuti successivi
leggono il prompt cachato (~80% risparmio sui token di input). Il logger
stampa `cache_read` ad ogni chiamata per monitorare hit rate.

**Tool use con `tool_choice="auto"`**: Claude decide se chiamare il tool
`register_transaction` (transazione) o rispondere con testo libero
(saluto, domanda). Forzare il tool sarebbe pericoloso: Claude
registrerebbe ANCHE messaggi non-transazione inquinando il giornale.
L'enum di `account_code` è caricato a startup da Supabase
(`init_claude_tool`) — il bot non parte se il fetch fallisce.

---

## 6. Variabili d'ambiente (Railway)

Tutte impostate dal pannello Railway > Variables. Lette in `bot.py` linee 44-48.

| Nome                    | Tipo     | Note                                              |
|-------------------------|----------|---------------------------------------------------|
| `TELEGRAM_TOKEN`        | required | Token del BotFather                               |
| `ANTHROPIC_API_KEY`     | required | Una sola chiave; bot usa `claude-sonnet-4-6`      |
| `SUPABASE_URL`          | required | `https://kwcznnsddtwhnfqtinvk.supabase.co`        |
| `SUPABASE_SERVICE_KEY`  | required | **service_role** key (RLS è OFF, ma keep secret)  |
| `SHEETS_URL`            | optional | URL del Google Apps Script. Vuoto = niente backup |

**Manca una di quelle required → il bot crasha all'avvio** con messaggio
esplicito (`validate_environment()`). Non parte in stato "metà funzionante".

---

## 7. Deploy

**Branch**: `BuongiornoEgitto-patch-1` (sì, "patch-1" — è il nome storico,
non un branch di hotfix). Railway è configurato per auto-deploy su push a
questo branch.

**Flusso di deploy**:
1. Modifica `bot.py` localmente.
2. `git diff bot.py` ← **leggere SEMPRE tutto il diff** (vedi gotcha sotto).
3. `git add bot.py && git commit -m "..."` con messaggio descrittivo.
4. `git push origin BuongiornoEgitto-patch-1`.
5. Railway redeploya in 1-2 minuti.
6. Smoke test in chat Telegram con la guida `Omar E` o un account di test.

**Rollback rapido**:
```
git revert --no-edit <hash>
git push origin BuongiornoEgitto-patch-1
```

**Schema Supabase**: le migrazioni `thoth-dashboard/db/migrations/*.sql` si
applicano A MANO dal Supabase SQL Editor. NON automatizzate. Numerate in
sequenza, applicare in ordine. Modificare il bot per assumere uno schema
nuovo senza prima aver applicato la migrazione = bot rotto.

---

## 8. Operazioni comuni

### Aggiungere una nuova guida
1. La guida scrive un messaggio qualsiasi al bot.
2. Bot risponde "Omar deve associarti a un conto".
3. (Opzionale ma raccomandato) Crea il suo conto cassa con migrazione SQL:
   ```sql
   INSERT INTO accounts (code, name, type, active, display_order)
   VALUES ('cassa_guida_<nome>', 'Cassa in mano a <Nome>', 'attivo', true, 15);
   ```
4. Mappa la riga in `telegram_users`:
   ```sql
   UPDATE telegram_users
   SET role = 'guida', account_code = 'cassa_guida_<nome>'
   WHERE display_name ILIKE '%<nome>%';
   ```
5. La guida riprova → ora può registrare.

### Aggiungere un nuovo conto economico (es. nuovo tipo di costo)
1. `INSERT INTO accounts (...)` con tipo `costo` o `ricavo`, `active = true`.
2. **Aggiornare `SYSTEM_PROMPT` in `bot.py`** (sezione "REGOLE ACCOUNT_CODE")
   per dire a Claude QUANDO usare il nuovo conto. L'enum dello schema viene
   popolato in automatico al boot (`fetch_active_economic_accounts`), ma se
   il prompt non descrive il conto Claude non saprà quando sceglierlo.
3. Commit + push → Railway redeploya, il bot al boot fetcha il nuovo enum.
   **Necessario riavvio**: l'enum è cachato process-wide.

### Backup Supabase
Supabase ha point-in-time restore (vedi piano), ma vale la pena un dump
periodico **off-platform** (`pg_dump` → S3 o GCS) per non dipendere dal
provider in caso di problemi. Vedi `app-deployment-safety` skill.

### Inspezionare un'entry sospetta
```sql
SELECT je.id, je.entry_date, je.description, je.source,
       jl.account_code, jl.dare, jl.avere, jl.currency
FROM journal_entries je
JOIN journal_lines jl ON jl.entry_id = je.id
WHERE je.id = '<uuid>'
ORDER BY jl.dare DESC;
```

### Reverse di una scrittura sbagliata
**Mai cancellare manualmente** righe da `journal_lines`/`journal_entries` —
spezza l'audit trail. Invece crea un'entry inversa:
- Per ogni riga originale, crea una riga con dare/avere scambiati.
- Aggiungi descrizione tipo "Storno entry <uuid> — motivo: ...".
- Per soft-delete c'è la migrazione 009 (`soft_delete_entries`): preferire
  quello quando applicabile, mantiene la riga ma la marca come cancellata.

---

## 9. Limiti noti / gotcha

1. **`callback_data` 64 byte**: se aggiungi un nuovo campo al callback delle
   tastiere, verifica che il nome di tutti gli account_code stia dentro.
2. **Telegram autocomplete cache lato client**: dopo aver cambiato
   `GUIDA_COMMANDS` o `ADMIN_COMMANDS`, gli utenti potrebbero vedere il menu
   vecchio fino a 1h. `BotCommandScopeChat` funziona solo se l'utente ha già
   interagito col bot in privato.
3. **Group chat**: il bot impara il primo `group_id` che vede e blocca tutti
   gli altri (`ALLOWED_GROUP_ID` impostato a runtime). Se lo aggiungi a un
   gruppo nuovo per sbaglio prima di quello giusto, devi riavviare il bot.
4. **Race condition su preview**: lo stato `_pending_previews` è in memoria,
   quindi se Railway riavvia il pod tutti i preview pendenti vanno persi.
   L'utente vedrà "preview scaduto" o simili. Accettabile per ora.
5. **REST non transazionale**: come spiegato sopra, una entry sbilanciata
   può esistere per qualche ms tra INSERT lines e RPC check se il processo
   muore in mezzo. Mitigato da `unbalanced_entries` view.
6. **Dual-write Sheet best-effort**: se il Sheet fallisce, l'entry su
   Supabase resta. Lo Sheet è solo un backup legacy, non bloccare niente.
7. **Cambio valuta a 4 righe**: il check `dare_xor_avere` impedisce di mettere
   dare e avere sulla stessa riga, ma NON impedisce a `dare/avere` di essere
   negativi. Validare lato applicazione (già fatto in `_do_cambio`).
8. **Guide vedono `/raccolgo` `/verso`**: dal commit `cd7138e` aperti a
   chiunque abbia un account_code. Se serve restringere di nuovo, riportare
   `_require_account_user` → `_require_admin` nei 4 entry point + 2 callback.
9. **Modello Claude**: hard-coded a `claude-sonnet-4-6`. Se Anthropic ritira
   il modello, il bot risponde "❌ Errore HTTP AI (400)" su ogni messaggio
   libero. Aggiornare `ANTHROPIC_MODEL` in `bot.py` linea 55 e redeployare.
10. **Enum `account_code` cachato a startup**: nuovi conti aggiunti via SQL
    NON sono visibili a Claude finché non si riavvia il bot. Per evitare di
    dover ricordare il restart, il flusso "aggiungere nuovo conto" sopra
    include esplicitamente questo step.
11. **`tool_choice="auto"` (non forzato)**: Claude può decidere di non
    chiamare il tool se il messaggio è ambiguo. La pre-filtro
    `_count_transaction_starts` blocca già i casi più ovvi (saluti senza
    +/-), ma se Claude rifiuta in modo creativo ("non sono sicuro che sia
    una transazione, riprova così") l'utente vede il testo grezzo. Si può
    monitorare con `stop_reason` nei log.

---

## 10. Quando aggiornare questo documento

Aggiornare **prima** di committare se:
- Aggiungi/rimuovi un comando slash.
- Cambi il flusso di parsing (es. nuovo trigger di preview).
- Modifichi lo schema DB (nuova migrazione, nuovo conto seed).
- Cambi la logica di auth (`_require_*`).
- Cambi env vars o segreti richiesti.
- Cambi modello Claude o provider AI.

NON serve aggiornare per: bug fix puntuali, refactoring interni, modifiche
ai messaggi utente. Il messaggio di commit basta.

---

## 11. File correlati

- `athos_contabile/bot.py` — il bot.
- `athos_contabile/Dockerfile`, `requirements.txt` — runtime.
- `thoth-dashboard/` — repo separato, dashboard Streamlit.
- `thoth-dashboard/db/migrations/*.sql` — schema authoritative.
- `thoth-dashboard/docs/` — note di design (DEPLOY_PARTITA_DOPPIA, FORNITORI_PLAN, ecc.).
- `~/.claude/projects/.../memory/` — preferenze e lessons-learned di Omar
  per le future sessioni Claude Code.
