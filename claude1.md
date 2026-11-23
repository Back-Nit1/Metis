 ▐▛███▛█   Claude Code v2.1.330
▝▜██████▀  Opus 5.5 · Claude Pro
 ▝▝   ▝▝   ~/Downloads/Metis


❯ Can you read the codebase and tell me exactly how RAG is implemented here?    
  One of the things that I never understood is: what type of data are we using? 
                                                                                
  I was thinking about how this works, and we have a desktop app of Metis, and  
  then we also have a browser version of it, right? I want to make a huge       
  infrastructure change because this is going to change up. This is going to    
  make a lot of issues. I think that I've messed up, and I don't think the      
  browser is going to work here.                                                
                                                                                
  The whole point of Metis is that it's supposed to be something local. It      
  should be like a desktop app that connects to your specific. It should be     
  able to pull all the files, data, and information from your computer, but     
  then it should also be able to pull information, like sync information,       
  together with Gmail, Drive, etc., right?                                      
                                                                                
  One thing that I'm currently wondering is that we need to be able to change   
  up everything so that we could see how RAG is involved here, because this     
  doesn't make sense. I know, also: what is this desktop app written in? Is it  
  written in Rust? That can change up a lot of things, so                       

  Searched for 1 pattern, read 1 file, listed 1 directory, ran 3 shell commands (ctrl+o to expand)

⏺ Reading where memories get created and how desktop shell talks to backend.

  Searched for 1 pattern, ran 2 shell commands (ctrl+o to expand)

⏺ How RAG works today

  What gets indexed: only "memories." Your files, emails and documents are not
  indexed. A memory is a short text unit made from an extraction item after you 
  accept a proposal:

  1. You speak or type a capture. It becomes a Source (only audio and text work;
     image and document are rejected with "not implemented yet", see
     services/api/src/metis_api/services.py:134).
  2. The LLM extracts items (thought, decision, work_log, task, event_intent,
     preference).
  3. When you accept, _record_memory
     (services/api/src/metis_api/proposals.py:490) maps each item to a memory
     kind (idea, decision, work_log, commitment, preference) with content =
     title + body. It then writes an outbox event, MEMORY_INDEX_REQUESTED.
  4. A worker (services/retrieval/.../handlers.py) embeds the content and
     upserts it. It skips the embedding if the content hash is already indexed.

  Where it's stored:
  - Local/desktop: a separate memory.sqlite. Vectors are kept as JSON text in a
    vectors table, plus an FTS5 table for keyword search.
  - Cloud: AWS S3 Vectors (no keyword search) with Bedrock Titan embeddings.

  Which embedder runs: build_embedder() chooses one of three.
  - cloud: Bedrock Titan.
  - local: Ollama /api/embed, but only if METIS_LOCAL_EMBED_MODEL is set.
  - fixture: a hashed bag of words, which is not semantic. This is the default
    in local mode. The desktop sets only METIS_MODE=local in the code I read, so
    unless you set the inference env vars yourself, desktop "RAG" is really
    FTS5 keyword search plus fake vectors.

  Query: a linear cosine scan over all your rows, with score = max(semantic, 
  lexical). Each hit is then checked against the main state store: deleted
  memories, stale source versions and other users' rows are dropped.

  What uses it:
  - The planner (generator.py:322 calls evidence_for_task): up to N evidence
    items per task, within one token budget for the whole plan.
  - The /search endpoint.

  Gap: memories_from_proposal in ingestion.py is written to keep the source text
  around each span, so phrases like "we abandoned this" aren't cut off. Nothing
  in production calls it. The API path stores only the extracted title + body.

  So the answer to "what data are we using": your own accepted thoughts,
  decisions and work logs, as short LLM-extracted snippets. There are no
  connectors for files, Gmail or Drive anywhere.

  What the desktop app is written in

  It's Tauri v2, so partly Rust:
  - Rust shell (apps/desktop/src-tauri/src/, about 4.2k lines): audio capture,
    HUD, text insertion, and a supervisor (stack.rs) that starts the backend
    processes.
  - UI: the same React web build. tauri.conf.json has frontendDist: 
    ../../web/dist, so the desktop app is the browser app running in a native
    window.
  - Backend: one bundled Python executable (metis-backend api|worker|speech)
    running locally on 127.0.0.1 with METIS_MODE=local and SQLite.

  On "the browser won't work"

  You haven't messed up as badly as it seems. The web UI isn't the problem. The
  browser can't read your disk, but it doesn't need to: the Python sidecar runs
  natively and has full filesystem access. A local-first design fits the current
  structure:

  - Desktop becomes the main product. The browser build is either dropped or
    kept as a remote view.
  - Add a connector layer in the Python backend, not in Rust. Each connector
    (local folders, Gmail, Drive) produces Sources. They then go through
    chunking, a real local embedding model (Ollama), and into memory.sqlite.
    Retrieval, evidence checks and the planner stay as they are.
  - Gmail/Drive: OAuth from the desktop app via a loopback redirect, with tokens
    stored in the macOS keychain (Tauri keyring). Sync runs incrementally on
    Gmail history IDs and Drive change tokens.
  - The RAG layer needs a split. Today's "memories" are your own distilled
    thoughts. Bulk documents and emails need their own chunk type with
    source-span references; otherwise they would swamp your notes. And
    ingestion.py needs to be wired in, not bypassed.

  This is a big change and it touches packages/contracts/ (new source kinds, a
  connector entity), which only the coordinator edits.

  Next step could be a design spec: data model for connectors and document
  chunks, sync jobs, and what happens to the web build. Want me to draft it?

✻ Sautéed for 59s · done Saturday 7:33 PM

❯ yes, draft the design spec                                                    

  Searched for 1 pattern, listed 3 directories (ctrl+o to expand)

⏺ Write(docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.m
       d)
  ⎿  Wrote 226 lines to docs/superpowers/specs/2026-11-21-metis-local-corpu
     s-connectors-design.md
      1 # Metis local corpus + connectors — design
      2 
      3 Status: **draft** 2026-11-21. Needs coordinator review (touches `
        packages/contracts/`).
      4 Supersedes nothing yet; would become ADR 0006 once accepted.
      5 
      6 ## 1. Problem
      7 
      8 Metis is meant to be a local-first assistant that knows what is o
        n your
     … +218 lines (ctrl+o to expand)

⏺ Spec written. Committing it.

  Ran 1 shell command (ctrl+o to expand)

⏺ I wrote the design spec as a draft and committed it to main (521b801):
  docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.md.

  Main decisions in it:
  - Desktop becomes the main product. The React build stays, because it's
    already the desktop UI. The hosted web and AWS stack get no corpus and no
    connectors (see Q1).
  - Connectors live in the Python sidecar, not Rust. That way they reuse the
    existing outbox, worker and retrieval code. Three are planned: local
    folders, Gmail and Drive.
  - Two separate retrieval pools. Memory stays as your own distilled notes. A
    new CorpusChunk holds verbatim slices of files and emails. Results are
    merged with a cap on the corpus share, so 50k emails can't drown out your
    own decisions.
  - Pipeline: sync job → SourceArtifact (with an origin field) → text extraction
    → chunking by structure (~500 tokens) → local embedding → sqlite-vec plus
    FTS5. Keyword and vector results are combined with reciprocal rank fusion,
    which replaces today's max(semantic, lexical).
  - Embeddings: I recommend running a small model inside the app (fastembed with
    bge-small). The alternative is Ollama. If the desktop is running the
    fixture embedder, the UI must say "keyword search only".
  - Privacy: indexing makes no paid model calls. Corpus text goes to Bedrock
    only if you turn that on per connector. OAuth tokens are stored in the macOS
    Keychain. Secret files like .ssh and .env are always excluded.
  - No automatic task extraction from emails or files. It only runs when you ask
    for it, and it goes through budgets and proposal review.
  - Phase 0 fixes current gaps first: connect the unused span-window chunker,
    and give the desktop a real embedder.

  Biggest risk: gmail.readonly and drive.readonly are restricted Google scopes.
  Releasing publicly requires Google's app verification plus a yearly CASA
  security assessment. Until then you're limited to 100 test users, and their
  tokens expire every 7 days.

  The spec needs your answers on:
  - Q1: keep the hosted stack as a phone-capture inbox, or retire it?
  - Q2: fastembed or Ollama for embeddings?
  - Q3: how far back Gmail should backfill.
  - Q4: single device only?
  - Q5: macOS only?

  It also adds new entities to packages/contracts/, which only the coordinator
  edits, so it has to go through them via handoff.

✻ Worked for 1m 37s · done Saturday 7:35 PM

※ recap: You're making Metis a local-first desktop app that indexes your files, Gmail and Drive for retrieval, and the design spec for that is drafted and committed. Next, answer its open questions, starting with whether to keep or retire the hosted web stack. (turn off recaps in /config)

❯ 1) lets retire the photo capture inbox that will consume a HUGE amount of     
  memory 2) lets do ollama for embeddings. 3) lets say gmail fills up from past 
  month up until current time (import dateimte library to always get the        
  current version of the backfilling here so we don't run into any potential    
  issues here as that is one of my concerns. I also think this should be only   
  single device for now. NO the support should not only be for macos as it      
  should be tht metis can be used for multiple types of laptops with various    
  different OS. i want you to implement dockers here in that case               

⏺ Updating spec with your answers. One pushback on Docker first, then edits.

  Listed 1 directory (ctrl+o to expand)

⏺ Update(docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.
        md)
  ⎿  Added 1 line, removed 1 line
      41  | --- | --- |
      42  | `apps/desktop` (Tauri, Rust) | Primary product. Window, HUD, 
          capture, sidecar supervisor, folder picker, OAuth browser hand-
          off. |
      43  | `apps/web` (React build) | **Stays** — it is the desktop UI (
          `frontendDist: ../../web/dist`). Still runnable in a browser ag
          ainst the local API for dev. |
      44 -| Hosted web + AWS stack | Frozen at current capability. No cor
         -pus, no connectors. Kept for capture-from-phone later or retire
         -d — open question Q1. |                                        
      44 +| Hosted web + AWS stack | **Retired** (decision D1). No new fe
         +atures; teardown is a separate, explicit ops step. New entities
         + need not be DynamoDB-compatible. |                            
      45  | Python backend sidecar | Owns connectors, parsing, chunking, 
          embedding, sync jobs. Already has filesystem access. |
      46  
      47  Rationale: the browser never needed disk access — the local Pyt
          hon sidecar

⏺ Update(docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.
        md)
  ⎿  Added 6 lines, removed 3 lines
      88  
      89  ### 4.2 Secrets
      90  
      91 -OAuth refresh tokens live in the **macOS Keychain** (Python `ke
         -yring`,                                                        
      92 -service `metis.connector.<id>`). Never in SQLite, never in the 
         -webview,                                                       
      93 -never in logs. Rule 8 (no credentials in the browser) holds.   
      91 +OAuth refresh tokens live in the **OS credential store** via Py
         +thon `keyring`                                                 
      92 +(service `metis.connector.<id>`): macOS Keychain, Windows Crede
         +ntial Locker,                                                  
      93 +Linux Secret Service (GNOME Keyring / KWallet). If no Linux sec
         +ret service is                                                 
      94 +running, the connector refuses to save the token and says so — 
         +no plaintext                                                   
      95 +fallback. Never in SQLite, never in the webview, never in logs.
         + Rule 8 (no                                                    
      96 +credentials in the browser) holds.                             
      97  
      98  ## 5. Pipeline
      99  

⏺ Update(docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.
        md)
  ⎿  Added 1 line, removed 1 line
      111  | Connector | Discovery | Incremental | Delete |
      112  | --- | --- | --- | --- |
      113  | `local_folder` | user picks folders in Tauri dialog; walk wi
           th include/exclude globs | poll every N min: compare `(mtime, 
           size)`, then sha256 | missing path → tombstone |
      114 -| `gmail` | `users.messages.list` with label/query filter, bou
          -nded backfill window (default 1 year) | `users.history.list` f
          -rom stored `historyId`; full resync if 404 | `messagesDeleted`
          - → tombstone |                                                
      114 +| `gmail` | `users.messages.list` with `q=after:<epoch>`, back
          +fill window = last 30 days, computed at sync time (§5.1.1) | `
          +users.history.list` from stored `historyId`; on 404 re-backfil
          +l with a freshly computed window | `messagesDeleted` → tombsto
          +ne |                                                          
      115  | `gdrive` | `files.list` in chosen folders, Google Docs expor
           ted as `text/plain` | `changes.list` with stored `pageToken` |
            `removed`/trashed → tombstone |
      116  
      117  Ponytail: polling first for local folders. FSEvents watcher on
           ly if polling

⏺ Update(docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.
        md)
  ⎿  Added 28 lines
      114  | `gmail` | `users.messages.list` with `q=after:<epoch>`, back
           fill window = last 30 days, computed at sync time (§5.1.1) | `
           users.history.list` from stored `historyId`; on 404 re-backfil
           l with a freshly computed window | `messagesDeleted` → tombsto
           ne |
      115  | `gdrive` | `files.list` in chosen folders, Google Docs expor
           ted as `text/plain` | `changes.list` with stored `pageToken` |
            `removed`/trashed → tombstone |
      116  
      117 +#### 5.1.1 Gmail backfill window (D3)                         
      118 +                                                              
      119 +The window is never a stored or hard-coded date. It is compute
          +d from the                                                    
      120 +current time every time a backfill starts:                    
      121 +                                                              
      122 +```python                                                     
      123 +from datetime import timedelta                                
      124 +                                                              
      125 +GMAIL_BACKFILL = timedelta(days=30)                           
      126 +                                                              
      127 +def gmail_backfill_query(clock: Clock) -> str:                
      128 +    # clock.now() is timezone-aware UTC (datetime.now(UTC) in 
          +production,                                                   
      129 +    # a controllable clock in tests/e2e). Gmail's after: accep
          +ts epoch seconds,                                             
      130 +    # which sidesteps the mailbox's timezone entirely.        
      131 +    since = clock.now() - GMAIL_BACKFILL                      
      132 +    return f"after:{int(since.timestamp())}"                  
      133 +```                                                           
      134 +                                                              
      135 +- Uses the injected `Clock` port (`metis_contracts.ports.Clock
          +`), which wraps                                               
      136 +  `datetime.now(UTC)`, rather than calling `datetime.now()` in
          +line. Same                                                    
      137 +  real-time behaviour, and e2e tests can pin the clock.       
      138 +- "Past month" = rolling 30 days, not calendar month (no month
          +-length edge cases).                                          
      139 +- First sync stores the newest `historyId`; after that only `h
          +istory.list`                                                  
      140 +  runs, so mail is never re-imported. Re-backfill (expired `hi
          +storyId`,                                                     
      141 +  reconnect) recomputes the window from *now*.                
      142 +- Mail older than 30 days is not imported. Widening the window
          + later is a                                                   
      143 +  config value, not a code change.                            
      144 +                                                              
      145  Ponytail: polling first for local folders. FSEvents watcher on
           ly if polling
      146  latency is a real complaint.
      147  

⏺ Update(docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.
        md)
  ⎿  Added 13 lines, removed 6 lines
      173  
      174  ### 5.4 Embeddings
      175  
      176 -Desktop must ship a real embedding model. Options:            
      176 +**Decision D2: Ollama.** Reuses the existing `LocalEmbedder`  
      177 +(`/api/embed` over loopback, dimension checked on every respon
          +se).                                                          
      178  
      178 -- **A (recommended): in-process ONNX** via `fastembed` with a 
          -small model                                                   
      179 -  (e.g. `bge-small-en-v1.5`, 384 dim, ~130 MB). Weights fetche
          -d at install time                                             
      180 -  pinned by SHA-256, same as speech weights (ADR 0004 §5). No 
          -extra app for the user.                                       
      181 -- B: existing `LocalEmbedder` (Ollama). Zero new code, but use
          -r must install                                                
      182 -  Ollama and pull a model.                                    
      179 +- Ollama runs natively on macOS, Windows and Linux; the user i
          +nstalls it once.                                              
      180 +- Default model `nomic-embed-text` (768 dim, ~270 MB). Set by 
          +the desktop                                                   
      181 +  supervisor as `METIS_LOCAL_EMBED_MODEL`; user can override. 
      182 +- Embedder selection is **decoupled from the LLM engine**: new
      183 +  `METIS_EMBED_ENGINE` (`auto|fixture|local`), so local embedd
          +ings don't force                                              
      184 +  local extraction. Today `build_embedder()` follows `METIS_IN
          +FERENCE_ENGINE`.                                              
      185 +- First run: desktop probes `127.0.0.1:11434`. Missing Ollama 
          +→ Sources screen                                              
      186 +  says "install Ollama for semantic search" with the download 
          +link; missing                                                 
      187 +  model → offers `ollama pull nomic-embed-text`. Until then, k
          +eyword-only.                                                  
      188 +- Ollama down mid-sync → chunks stay `pending_embedding`, retr
          +ied by the                                                    
      189 +  outbox; FTS5 keyword search still works on them.            
      190  
      191  Fixture embedder stays for tests only. If desktop is running f
           ixture or the
      192  model is missing, UI must say "keyword search only" (rule 3, h
           onest states).

⏺ Update(docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.
        md)
  ⎿  Added 51 lines, removed 1 line
      244    and open the original (file path reveal, Gmail/Drive deep li
           nk).
      245  - Index status: "Indexing 1,240 / 8,000", "keyword only", "nee
           ds re-auth".
      246  
      247 -## 10. Delivery phases                                        
      247 +## 10. Cross-platform: macOS, Windows, Linux (D5)             
      248  
      249 +Metis ships as a **native installer per OS**. Everything in th
          +e stack is                                                    
      250 +already portable:                                             
      251 +                                                              
      252 +| Layer | macOS | Windows | Linux |                           
      253 +| --- | --- | --- | --- |                                     
      254 +| Shell (Tauri v2) | `.app` / `.dmg` | `.msi` / NSIS `.exe` | 
          +`.deb` / `.AppImage` |                                        
      255 +| Backend sidecar (PyInstaller, per target triple, `build.sh` 
          +already handles `.exe`) | arm64 + x86_64 | x86_64 | x86_64 |  
      256 +| Embeddings | Ollama | Ollama | Ollama |                     
      257 +| Secrets | Keychain | Credential Locker | Secret Service |   
      258 +| App data | `~/Library/Application Support/Metis` | `%APPDATA
          +%\Metis` | `$XDG_DATA_HOME/metis` |                           
      259 +                                                              
      260 +Work needed:                                                  
      261 +- `tauri.conf.json` `bundle.targets` is `["app"]` (macOS only)
          + → add                                                        
      262 +  `dmg, msi, nsis, deb, appimage`.                            
      263 +- Rust shell OS-specific code: audio capture, global hotkey, t
          +ext insertion                                                 
      264 +  (`insertion.rs`), HUD window behaviour. Each needs a Windows
          +/Linux path or an                                             
      265 +  honest "not supported on this OS" state. Linux Wayland restr
          +icts global                                                   
      266 +  hotkeys and synthetic typing — likely degraded there.       
      267 +- Paths through `pathlib` only; default exclude list per OS (§
          +5.1).                                                         
      268 +- CI matrix (GitHub Actions `macos-14`, `windows-latest`, `ubu
          +ntu-22.04`)                                                   
      269 +  builds and tests each installer. PyInstaller cannot cross-co
          +mpile, so each OS                                             
      270 +  must build its own sidecar.                                 
      271 +                                                              
      272 +### 10.1 Docker — where it fits, where it doesn't             
      273 +                                                              
      274 +Requested: use Docker for multi-OS support. **Recommendation: 
          +do not run the                                                
      275 +end-user app in Docker.** Reasons:                            
      276 +                                                              
      277 +- On macOS and Windows, Docker runs a Linux VM. Your files, mi
          +crophone,                                                     
      278 +  keychain, global hotkeys and text insertion are all on the h
          +ost. A container                                              
      279 +  can't reach them without bind mounts and device passthrough,
          + and Windows and                                              
      280 +  macOS don't support microphone passthrough at all.          
      281 +- Users would have to install Docker Desktop (~2 GB plus a VM 
          +that keeps                                                    
      282 +  several GB of RAM reserved). That's the memory cost D1 was t
          +rying to avoid.                                               
      283 +  Docker Desktop also needs a paid licence at larger companies
          +.                                                             
      284 +- Ollama inside Docker on a Mac gets no GPU (Metal), so embedd
          +ings are much slower.                                         
      285 +                                                              
      286 +Docker **is** used for:                                       
      287 +1. **Reproducible Linux builds.** A `Dockerfile.build` pins De
          +bian, Python,                                                 
      288 +   uv, Rust and the Tauri dependencies to produce the Linux si
          +decar and                                                     
      289 +   `.deb`/`.AppImage`. CI and devs get identical artefacts.   
      290 +2. **Headless dev/test stack.** `docker compose up` runs the A
          +PI, worker,                                                   
      291 +   speech (existing `services/speech/Dockerfile`) and Ollama w
          +ith fixture                                                   
      292 +   adapters, so contributors on any OS can run backend tests w
          +ithout a local                                                
      293 +   Python setup. Dev only; never shipped to users.            
      294 +3. **Optional power-user mode (later, not in scope).** Linux u
          +sers who want                                                 
      295 +   the backend in a container on a home server. Single-device 
          +rule (D4) still holds.                                        
      296 +                                                              
      297 +## 11. Delivery phases                                        
      298 +                                                              
      299  | Phase | Scope | Gate |
      300  | --- | --- | --- |
      301  | 0 | Fix today: wire span-window chunking into `_record_memor
           y`; desktop uses a real embedder or shows "keyword only" | ret
           rieval eval (`evaluate.py`) improves on paraphrase queries |

⏺ Update(docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.
        md)
  ⎿  Added 2 lines, removed 1 line
      298  
      299  | Phase | Scope | Gate |
      300  | --- | --- | --- |
      301 -| 0 | Fix today: wire span-window chunking into `_record_memor
          -y`; desktop uses a real embedder or shows "keyword only" | ret
          -rieval eval (`evaluate.py`) improves on paraphrase queries |  
      301 +| 0 | Fix today: wire span-window chunking into `_record_memor
          +y`; `METIS_EMBED_ENGINE` + Ollama probe in desktop, "keyword o
          +nly" otherwise | retrieval eval (`evaluate.py`) improves on pa
          +raphrase queries |                                            
      302 +| 0b | Cross-platform builds: bundle targets, CI matrix, `Dock
          +erfile.build` for Linux, dev `docker-compose.yml` | installers
          + build and launch on all three OSes; backend tests pass in com
          +pose |                                                        
      303  | 1 | Contracts (Connector, SourceOrigin, CorpusChunk, events)
           ; sqlite-vec store; corpus lane + RRF | unit + eval green; mem
           ory-only behaviour unchanged |
      304  | 2 | `local_folder` connector + parsers + chunker + Sources U
           I | index a 5k-file folder; incremental resync < 30 s with no 
           changes |
      305  | 3 | Gmail connector + OAuth + keychain | backfill 1 year; hi
           story sync; delete propagates |

⏺ Now replacing the open questions with the decisions, plus the two items still
  open.

⏺ Bash(f=docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.
      md && python3 - "$f" <<'EOF'
      import sys…)
  ⎿  6:## 1. Problem
     22:## 2. Goals / non-goals
     38:## 3. Decision: platform shape
     … +11 lines (ctrl+o to expand)
  ⎿  Updated docs/superpowers/specs/2026-11-21-metis-local-corpus-connector
     s-design.md +18 -7
      1  # Metis local corpus + connectors — design
      2  
      3 -Status: **draft** 2026-11-21. Needs coordinator review (touches 
        -`packages/contracts/`).                                         
      3 +Status: **draft** 2026-11-21, decisions D1–D5 recorded §12. Need
        +s coordinator review (touches `packages/contracts/`).           
      4  Supersedes nothing yet; would become ADR 0006 once accepted.
      5  
      6  ## 1. Problem
     ...
      306  | 4 | Drive connector | changes feed; Docs export; trash propa
           gates |
      307  | 5 | Opt-in extraction from corpus ("make tasks from this") |
            budget + proposal review path, no auto-created tasks |
      308  
      309 -## 11. Open questions                                         
      309 +## 12. Decisions (2026-11-21)                                 
      310  
      311 -- **Q1** Hosted web/AWS stack: keep as phone-capture inbox tha
          -t syncs to desktop, or retire? Affects whether contracts must 
          -stay DynamoDB-compatible for new entities.                    
      312 -- **Q2** Embedding: fastembed in-process (A) vs Ollama (B)? Af
          -fects bundle size (~130 MB) and onboarding.                   
      313 -- **Q3** Default Gmail backfill window and label filter (all m
          -ail vs Inbox/Sent only).                                      
      314 -- **Q4** Multi-device: is the desktop install the single sourc
          -e of truth, or do we need sync between Macs later? (This desig
          -n assumes single device.)                                     
      315 -- **Q5** Windows/Linux in scope, or macOS only for now? (Keych
          -ain + folder defaults differ.)                                
      311 +- **D1** Hosted web/AWS stack (phone-capture inbox) retired. T
          +eardown of the                                                
      312 +  deployed AWS resources is a separate, explicitly approved op
          +s step.                                                       
      313 +- **D2** Embeddings via Ollama (`nomic-embed-text` default), s
          +ee §5.4.                                                      
      314 +- **D3** Gmail backfill = rolling last 30 days, computed from 
          +the current time                                              
      315 +  at every backfill (§5.1.1); then incremental via `historyId`
          +.                                                             
      316 +- **D4** Single device. The desktop install is the only source
          + of truth; no                                                 
      317 +  cross-device sync.                                          
      318 +- **D5** macOS, Windows and Linux. Native installers; Docker o
          +nly for builds and                                            
      319 +  the dev stack (§10.1).                                      
      320 +                                                              
      321 +## 13. Still open                                             
      322 +                                                              
      323 +- **Q6** With AWS retired, LLM extraction on desktop has no Be
          +drock. Choose:                                                
      324 +  local Ollama LLM (free, slower, weaker), or direct Anthropic
          + API with a                                                   
      325 +  user-supplied key stored in the OS credential store.        
      326 +- **Q7** Linux Wayland: accept degraded hotkey/insertion, or t
          +arget X11 only at first?                                      

⏺ I've added your answers to the spec and committed it (1743692). It's still
  only the design doc: I haven't written any code yet, including any Docker
  files.

  Where I pushed back: I didn't put the end-user app in Docker. On macOS and
  Windows, Docker runs a Linux virtual machine, and from there it can't reach
  your files, mic, keychain, hotkeys or text insertion. It would also make every
  user install Docker Desktop, which is about 2 GB and keeps several GB of RAM
  reserved. That's the same memory cost you wanted to avoid in decision 1. And
  Ollama in Docker on a Mac can't use the GPU, so embeddings would be much
  slower.

  Here's what the spec now says for each answer:

  1. Hosted stack retired. No new features for the web/AWS version. Shutting
     down the deployed AWS resources is left as a separate step that you approve
     explicitly; I haven't touched it.
  2. Ollama for embeddings. Default model is nomic-embed-text. A new
     METIS_EMBED_ENGINE setting lets embeddings run locally without forcing the
     AI extraction step to be local too. On first run the app checks whether
     Ollama is installed. If it isn't, it says so and falls back to keyword-only
     search.
  3. Gmail backfill covers the last 30 days, worked out from the current time 
     every time a backfill starts. It's never a stored date:
  since = clock.now() - timedelta(days=30)
  q = f"after:{int(since.timestamp())}"
     It uses the project's existing Clock port, which wraps datetime.now(UTC).
     That gives the same real-time behaviour as calling datetime directly, but
     tests can pin the clock. After the first sync, Gmail's change history
     (historyId) keeps it up to date. Any re-backfill recomputes the window from
     the current time.
  4. Single device. The desktop install is the only copy of your data; nothing
     syncs between machines.
  5. macOS, Windows and Linux. The app ships as a native installer for each OS
     (Tauri already supports all three), built on each OS in CI. The work
     needed:
     - The bundle settings currently only build the macOS app.
     - The Rust audio, hotkey and text insertion code needs Windows and Linux
       versions.
     - Secrets go in each OS's credential store.
     - Docker is used where it actually helps:
       - a pinned build container for the Linux installer;
       - a docker compose dev stack (backend plus Ollama) so contributors on any
         OS can run the backend tests.

  Two things are still open in the spec:
  - Q6: retiring AWS also removes Bedrock, which the AI extraction step runs on.
    It needs a replacement: Ollama locally, or the Anthropic API with your own
    key.
  - Q7: on Linux, the newer Wayland display system restricts global hotkeys and
    simulated typing. Do we accept that those work less well there, or start
    with the older X11 only?

  Once you've answered those, I'd start with phase 0 (connect the unused chunker
  and add the Ollama check) and phase 0b (cross-platform builds).

✻ Cogitated for 1m 15s · done Saturday 9:00 PM

❯ where is the spec exactly?                                                    
⏺ The spec is at:

  /Users/nityanthmaramreddy/Downloads/Metis/docs/superpowers/specs/2026-11-21-me
  tis-local-corpus-connectors-design.md

  It's on main, in commits 521b801 (first draft) and 1743692 (your decisions
  D1–D5). It sits next to the earlier desktop spec,
  2026-10-06-metis-desktop-push-to-talk-design.md.

  To open it:
  ! open 
  docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.md

✻ Worked for 2s · done Saturday 9:02 PM

❯ import langchain for the code and syntax btw. i wat us to implemenet hybrid   
  retreieval into the entire workflow. for example there must be semantic       
  search + keyword search. (                                                    
                                                                                
  2. Command Line / Terminal                                                    
                                                                                
  If you just want to pull filenames quickly from your OS terminal:             
  • macOS / Linux (find):bash                                                   
  Find files with "report" in the name (case-insensitive)                       
                                                                                
  find ./my_folder -type f -iname "report"                                      
  Use code with caution.                                                        
  ) <----this is because I want to implmement a wait for us to use hybrid       
  retreieval here for my accurate searches and then there should be surfacing   
  those citations to us when conducting searches. then i would also want to     
  implemment a queue in order to prevent the workers initially from getting     
  overwhelemed. implement the queus and then we should make a Dead letter queue 
  in the case that the files or pdfs provided (or even email contents           
  themselves) end up getting malformed. the queue should be able to handle      
  everything from the vairous different data streams. I also want us to         
  to have a way to be able to queue this so we don't overwhelm the workers. The 
  thing is, multiple Python workers are going to be handling this.              
                                                                                
  I also want us to implement some type of way where the frontend reflects      
  changes to our backend. Specifically, that needs to be there, like that       
  syncing. There has to be a worker. All the background tasks should be synced. 
  There should be background workers that sync up the entire desktop app to     
  the current version of your platform, or the current version of where you're  
  at in terms of the data streams.                                              
                                                                                
  That should not be on the main page, because otherwise that will cause        
  latency issues where it'll end up lagging our computer.                       
                                                                                
  Besides that, make sure I think we've written the app, the desktop, in Rust   
  specifically, but that's the only thing I would say to change up with the     
  spec. Re-update this back, and then start building this.                      

⏺ Skill(ponytail)
  ⎿  Loaded skill

  Listed 2 directories, ran 1 shell command (ctrl+o to expand)

⏺ Existing queue already there (outbox with leases, retries, dead_letter state).
  Checking deps + network before LangChain.

  Listed 2 directories, ran 3 shell commands (ctrl+o to expand)

⏺ Big build. Mapping remaining touchpoints: contracts, state store, API search.

  Read 1 file, ran 4 shell commands (ctrl+o to expand)

⏺ Bash(f=docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.
      md && python3 - "$f" <<'EOF'
      import sys…)
  ⎿  0f2af3a docs: add LangChain hybrid retrieval, queue lanes, DLQ and syn
     c decisions
  ⎿  Updated docs/superpowers/specs/2026-11-21-metis-local-corpus-connector
     s-design.md +55 -2
      1  # Metis local corpus + connectors — design
      2  
      3 -Status: **draft** 2026-11-21, decisions D1–D5 recorded §12. Need
        -s coordinator review (touches `packages/contracts/`).           
      3 +Status: **draft** 2026-11-21, decisions D1–D10 recorded §12, bui
        +ld notes §14. Needs coordinator review (touches `packages/contra
        +cts/`).                                                         
      4  Supersedes nothing yet; would become ADR 0006 once accepted.
      5  
      6  ## 1. Problem
     ...
      39  
      40  | Surface | Role after this change |
      41  | --- | --- |
      42 -| `apps/desktop` (Tauri, Rust) | Primary product. Window, HUD, 
         -capture, sidecar supervisor, folder picker, OAuth browser hand-
         -off. |                                                         
      42 +| `apps/desktop` (**Rust**, Tauri v2) | Primary product, writte
         +n in Rust. The Rust shell owns the native layer. Window, HUD, c
         +apture, sidecar supervisor, folder picker, OAuth browser hand-o
         +ff. |                                                          
      43  | `apps/web` (React build) | **Stays** — it is the desktop UI (
          `frontendDist: ../../web/dist`). Still runnable in a browser ag
          ainst the local API for dev. |
      44  | Hosted web + AWS stack | **Retired** (decision D1). No new fe
          atures; teardown is a separate, explicit ops step. New entities
           need not be DynamoDB-compatible. |
      45  | Python backend sidecar | Owns connectors, parsing, chunking, 
          embedding, sync jobs. Already has filesystem access. |
     ...
      318  - **D5** macOS, Windows and Linux. Native installers; Docker o
           nly for builds and
      319    the dev stack (§10.1).
      320  
      321 +- **D6** The desktop app is **Rust** (Tauri v2). The React bui
          +ld is only its                                                
      322 +  view layer; native capability (audio, hotkeys, insertion, pr
          +ocess                                                         
      323 +  supervision, OS integration) stays in Rust.                 
      324 +- **D7** **LangChain** is the retrieval vocabulary: `langchain
          +-core`                                                        
      325 +  (`BaseRetriever`, `Document`, `Embeddings`), `langchain-text
          +-splitters`                                                   
      326 +  (`RecursiveCharacterTextSplitter`) and `langchain-ollama`   
      327 +  (`OllamaEmbeddings`). Not `langchain-classic`/`langchain-com
          +munity`: they                                                 
      328 +  pull SQLAlchemy, LangSmith and hundreds of loaders into the 
          +bundled sidecar.                                              
      329 +  Rank fusion is the same weighted reciprocal-rank-fusion `Ens
          +embleRetriever`                                               
      330 +  uses, implemented inside our own `HybridRetriever`.         
      331 +- **D8** **Hybrid retrieval everywhere.** Every search and eve
          +ry planner                                                    
      332 +  evidence lookup runs semantic (vector cosine) **and** keywor
          +d (SQLite FTS5                                                
      333 +  BM25) search, then fuses them. Keyword search covers file na
          +mes and paths                                                 
      334 +  too (the `find -iname "*report*"` case), so a file is findab
          +le by name even                                               
      335 +  when its body never mentions the word. Every result is a **c
          +itation**: title,                                             
      336 +  path/URI, excerpt, character span, source id + version, and 
          +which matchers                                                
      337 +  hit (`semantic`, `keyword`).                                
      338 +- **D9** **Queued ingestion with lanes and a dead-letter queue
          +.** All data                                                  
      339 +  streams (captures, files, PDFs, email, Drive) flow through t
          +he one                                                        
      340 +  transactional outbox. Events are split into two lanes:      
      341 +  `interactive` (captures, extraction, reminders, plans) and `
          +ingest`                                                       
      342 +  (connector sync, file/email parsing, chunk embedding). Sever
          +al Python worker                                              
     … +37 lines (ctrl+o to expand)

⏺ Spec updated (D6–D10). Now building — step 1: queue lanes + multi-process
  workers + DLQ API. Baseline tests first.

  Ran 2 shell commands (ctrl+o to expand)

❯ spin up subagents to amek it easier to build this entire thin g               

⏺ Got it — will split into parallel subagents. First, contracts must land
  (single source of truth, only coordinator edits), then agents work in isolated
  worktrees on disjoint paths.

  Ran 3 shell commands (ctrl+o to expand)

⏺ Baseline: 571 passed. Writing contracts now.

  Ran 3 shell commands (ctrl+o to expand)

⏺ Now writing contract edits.

⏺ Bash(python3 - <<'EOF'
      import re…)
  ⎿  52:    SourceArtifact,
     94:from .events import (
     176:    "SourceArtifact",
  ⎿  Updated packages/contracts/src/metis_contracts/common.py +10
      121      excerpt: Annotated[str, Field(max_length=2000)]
      122      recorded_at: datetime
      123      validity: Literal["current", "historical", "superseded", "
           uncertain"] = "current"
      124 +    #: Citation fields. ``origin`` says where the excerpt came
          + from so a file or                                            
      125 +    #: email excerpt is never presented as the user's own deci
          +sion.                                                         
      126 +    origin: Literal["memory", "file", "email", "drive"] = "mem
          +ory"                                                          
      127 +    chunk_id: EntityId | None = None                          
      128 +    title: Annotated[str | None, Field(default=None, max_lengt
          +h=500)] = None                                                
      129 +    uri: Annotated[str | None, Field(default=None, max_length=
          +2000)] = None                                                 
      130 +    """File path, or a provider deep link. Never a presigned o
          +r credentialed URL."""                                        
      131 +    start_char: Annotated[int | None, Field(default=None, ge=0
          +)] = None                                                     
      132 +    end_char: Annotated[int | None, Field(default=None, ge=0)]
          + = None                                                       
      133 +    matched: list[Literal["semantic", "keyword"]] = Field(defa
          +ult_factory=list)                                             
      134  
      135  
      136  class SourceSpan(StrictModel):
  ⎿  Updated packages/contracts/src/metis_contracts/entities.py +53
      26  from .enums import (
      27      AdapterMode,
      28      AttendanceStatus,
      29 +    ConnectorKind,                                             
      30 +    ConnectorStatus,                                           
      31      DeliveryChannel,
      32      DeliveryStatus,
      33      ExtractedItemKind,
     ...
      107      content_type: str | None = None
      108  
      109  
      110 +class SourceOrigin(StrictModel):                              
      111 +    """Where an imported source came from. ``external_version`
          +` changing is what                                            
      112 +    bumps ``SourceArtifact.current_version``."""              
      113 +                                                              
      114 +    connector_id: EntityId                                    
      115 +    external_id: Annotated[str, Field(min_length=1, max_length
          +=2000)]                                                       
      116 +    """Absolute path for files, message id for email, file id 
          +for Drive."""                                                 
      117 +    external_version: Annotated[str, Field(min_length=1, max_l
          +ength=200)]                                                   
      118 +    """mtime+size+sha256 for files, historyId/etag for provide
          +rs."""                                                        
      119 +    uri: Annotated[str, Field(max_length=2000)]               
      120 +    title: Annotated[str, Field(max_length=500)]              
      121 +    mime_type: str | None = None                              
      122 +    modified_at: datetime | None = None                       
      123 +    size_bytes: Annotated[int | None, Field(default=None, ge=0
          +)] = None                                                     
      124 +                                                              
      125 +                                                              
      126  class SourceArtifact(Record):
      127      """Evidence of what the user said or imported. Immutable p
           er source_version."""
      128  
     ...
      145      error_message: Annotated[str | None, Field(default=None, m
           ax_length=1000)] = None
      146      project_id: EntityId | None = None
      147      """Project this capture was explicitly attached to by the 
           user; never inferred."""
      148 +    origin: SourceOrigin | None = None                        
      149 +    """Set only for connector-imported sources (file, email, d
          +rive_file)."""                                                
      150 +    chunk_count: Annotated[int | None, Field(default=None, ge=
          +0)] = None                                                    
      151 +    """Corpus chunks indexed for ``current_version``; None unt
          +il ingested."""                                               
      152  
      153      def current_text(self) -> str | None:
      154          for t in self.transcripts:
     … +41 lines (ctrl+o to expand)
  ⎿  Updated packages/contracts/src/metis_contracts/enums.py +17
      14      TEXT = "text"
      15      IMAGE = "image"
      16      DOCUMENT = "document"
      17 +    #: Imported by a connector. Never captured by the user dire
         +ctly.                                                          
      18 +    FILE = "file"                                              
      19 +    EMAIL = "email"                                            
      20 +    DRIVE_FILE = "drive_file"                                  
      21 +                                                               
      22 +                                                               
      23 +class ConnectorKind(StrEnum):                                  
      24 +    LOCAL_FOLDER = "local_folder"                              
      25 +    GMAIL = "gmail"                                            
      26 +    GDRIVE = "gdrive"                                          
      27 +                                                               
      28 +                                                               
      29 +class ConnectorStatus(StrEnum):                                
      30 +    ACTIVE = "active"                                          
      31 +    PAUSED = "paused"                                          
      32 +    NEEDS_AUTH = "needs_auth"                                  
      33 +    ERROR = "error"                                            
      34  
      35  
      36  class SourceLifecycle(StrEnum):
  ⎿  Updated packages/contracts/src/metis_contracts/events.py +34
      44      #: Provider fired. payload: {"local_date": "YYYY-MM-DD", "s
          cheduler_ref"}.
      45      #: Never trusted: the API re-derives date and validity from
           preferences.
      46      DAILY_PLAN_DUE = "plan.daily_due"
      47 +    #: Ingest lane. payload: ConnectorSyncPayload. Scans one co
         +nnector and                                                    
      48 +    #: raises SOURCE_INGEST_REQUESTED per new/changed item.    
      49 +    CONNECTOR_SYNC_REQUESTED = "connector.sync_requested"      
      50 +    #: Ingest lane. payload: SourceIngestPayload. Parse, chunk,
         + embed one source.                                             
      51 +    #: Malformed input is permanent (dead letter), never retrie
         +d.                                                             
      52 +    SOURCE_INGEST_REQUESTED = "source.ingest_requested"        
      53 +    #: Ingest lane. payload: PurgePayload (target_kind="source"
         +). Removes corpus chunks.                                      
      54 +    CORPUS_PURGE_REQUESTED = "corpus.purge_requested"          
      55 +                                                               
      56 +                                                               
      57 +#: Events on the ingest lane. Everything else is interactive. T
         +he ingest lane                                                 
      58 +#: has a global in-flight cap so bulk imports never starve remi
         +nders.                                                         
      59 +INGEST_EVENT_TYPES: frozenset[str] = frozenset(                
      60 +    {                                                          
      61 +        EventType.CONNECTOR_SYNC_REQUESTED,                    
      62 +        EventType.SOURCE_INGEST_REQUESTED,                     
      63 +        EventType.CORPUS_PURGE_REQUESTED,                      
      64 +    }                                                          
      65 +)                                                              
      66  
      67  
      68  ALL_EVENT_TYPES: frozenset[str] = frozenset(
     ...
      208      planning_epoch: Annotated[int, Field(ge=0)]
      209  
      210  
      211 +class ConnectorSyncPayload(StrictModel):                      
      212 +    connector_id: EntityId                                    
      213 +    full: bool = False                                        
      214 +    """True re-scans everything (reconnect, expired cursor).""
          +"                                                             
      215 +                                                              
      216 +                                                              
      217 +class SourceIngestPayload(StrictModel):                       
      218 +    source_id: EntityId                                       
      219 +    source_version: Revision                                  
      220 +    connector_id: EntityId                                    
      221 +                                                              
      222 +                                                              
     … +12 lines (ctrl+o to expand)

  Ran 2 shell commands · 1 failed (ctrl+o to expand)

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis && uv run --frozen ruff
      check --fix -q packages/contracts && uv run --frozen ruff format -q
      packages/contracts && m…)
  ⎿  ......................................................................
     .. [ 75%]
     ......................................................................
     … +4 lines (ctrl+o to expand)
  ⎿  Updated ops/gcp/README.md +4 -3
      45  
      46  This creates the `Metis API HTTPS` uptime check, `metis_service
          _errors`
      47  log-based metric, CPU/memory/uptime/service-error alert policie
          s, and the
      48 -`Metis Operations` dashboard. Add a notification channel in Clo
         -ud Monitoring                                                  
      49 -and attach it to the policies; policies without a channel creat
         -e visible                                                      
      50 -incidents but send no email, SMS, Slack, or PagerDuty notificat
         -ion.                                                           
      48 +`Metis Operations` dashboard. Set `NOTIFY_EMAIL=you@example.com
         +` to create (or                                                
      49 +reuse) an email notification channel and attach it to every pol
         +icy. Without it,                                               
      50 +policies create visible incidents but send no email, SMS, Slack
         +, or PagerDuty                                                 
      51 +notification.                                                  
      52  
      53  - [VM details](https://console.cloud.google.com/compute/instanc
          esDetail/zones/us-central1-a/instances/metis?project=metis-dev-
          856337)
      54  - [Monitoring dashboards](https://console.cloud.google.com/moni
          toring/dashboards?project=metis-dev-856337)
  ⎿  Updated ops/gcp/configure-monitoring.sh +2 -1
      66  if [ -n "${NOTIFY_EMAIL:-}" ]; then
      67    api="https://monitoring.googleapis.com/v3/projects/$PROJECT/n
          otificationChannels"
      68    auth="Authorization: Bearer $(gcloud auth print-access-token)
          "
      69 -  json_name() { python3 -c 'import json,sys; d=json.load(sys.st
         -din); d=(d.get("notificationChannels") or [{}])[0] if "notifica
         -tionChannels" in d or not d.get("name") else d; print(d.get("na
         -me",""))'; }                                                   
      69 +  # List returns {"notificationChannels": [...]} (or {}); creat
         +e returns the channel.                                         
      70 +  json_name() { python3 -c 'import json,sys; d=json.load(sys.st
         +din); print((d.get("notificationChannels") or [d])[0].get("name
         +",""))'; }                                                     
      71    channel="$(curl -fsS -H "$auth" -G "$api" \
      72      --data-urlencode "filter=type=\"email\" AND labels.email_ad
          dress=\"$NOTIFY_EMAIL\"" | json_name)"
      73    if [ -z "$channel" ]; then
  ⎿  Updated ops/gcp/monitoring/dashboard.json +5 -20
      26                  }
      27                }
      28              ],
      29 -            "yAxis": {                                         
      30 -              "label": "utilization",                          
      31 -              "scale": "LINEAR"                                
      32 -            }                                                  
      29 +            "yAxis": {"label": "utilization", "scale": "LINEAR"
         +}                                                              
      30            }
      31          }
      32        },
     ...
      52                  }
      53                }
      54              ],
      58 -            "yAxis": {                                         
      59 -              "label": "percent",                              
      60 -              "scale": "LINEAR"                                
      61 -            }                                                  
      55 +            "yAxis": {"label": "percent", "scale": "LINEAR"}   
      56            }
      57          }
      58        },
     ...
      80                  }
      81                }
      82              ],
      89 -            "yAxis": {                                         
      90 -              "label": "passing fraction",                     
      91 -              "scale": "LINEAR"                                
      92 -            }                                                  
      83 +            "yAxis": {"label": "passing fraction", "scale": "LI
         +NEAR"}                                                         
      84            }
      85          }
      86        },
     ...
      106                  }
      107                }
      108              ],
      118 -            "yAxis": {                                        
      119 -              "label": "errors/second",                       
      120 -              "scale": "LINEAR"                               
      121 -            }                                                 
     … +15 lines (ctrl+o to expand)
  ⎿  Updated packages/contracts/src/metis_contracts/__init__.py +6 -6
      114  
      115  __all__ = [
      116      "ALL_EVENT_TYPES",
      117 -    "INGEST_EVENT_TYPES",                                     
      118 -    "Connector",                                              
      119 -    "ConnectorConfig",                                        
      120 -    "ConnectorKind",                                          
      121 -    "ConnectorStatus",                                        
      122 -    "SourceOrigin",                                           
      117      "CONTRACT_VERSION",
      118      "ENTITY_MODELS",
      119      "HTTP_STATUS",
      120 +    "INGEST_EVENT_TYPES",                                     
      121      "PAYLOAD_MODELS",
      122      "REMINDER_ACTIVE_STATES",
      123      "REMINDER_TRANSITIONS",
     ...
      136      "BudgetReservation",
      137      "Budgets",
      138      "ConfidenceHint",
      139 +    "Connector",                                              
      140 +    "ConnectorConfig",                                        
      141 +    "ConnectorKind",                                          
      142 +    "ConnectorStatus",                                        
      143      "Decision",
      144      "DeliveryChannel",
      145      "DeliveryStatus",
     ...
      187      "SourceArtifact",
      188      "SourceKind",
      189      "SourceLifecycle",
      190 +    "SourceOrigin",                                           
      191      "SourceRef",
      192      "SourceSpan",
      193      "SpeechMetadata",
  ⎿  Updated packages/contracts/src/metis_contracts/api.py +1 -1
      24  )
      25  from .entities import (
      26      AvailabilityBlock,
      27 +    Budgets,                                                   
      28      Connector,
      29      ConnectorConfig,
      29 -    Budgets,                                                   
      30      Event,
      31      ExtractedItem,
      32      ExtractionProposal,
  ⎿  (timeout 10m)

⏺ Contracts green (571 pass, web typecheck clean). Adding LangChain + pypdf deps
  centrally so agents don't fight over uv.lock.

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis && sed -i ''
      's/^dependencies = \["metis-contracts", "metis-state",
      "metis-pipeline"\]/dependencies = [\n  "metis-c…)
  ⎿  [project]
     name = "metis-retrieval"
     version = "0.1.0"
     … +22 lines (ctrl+o to expand)
  ⎿  Updated services/retrieval/pyproject.toml +10 -1
       3  version = "0.1.0"
       4  description = "Metis semantic memory: ingestion, embeddings and
           owner-scoped retrieval"
       5  requires-python = ">=3.12"
       6 -dependencies = ["metis-contracts", "metis-state", "metis-pipeli
         -ne"]                                                           
       6 +dependencies = [                                               
       7 +  "metis-contracts",                                           
       8 +  "metis-state",                                               
       9 +  "metis-pipeline",                                            
      10 +  # D7: LangChain vocabulary for retrieval. Not langchain-class
         +ic/community (bundle size).                                    
      11 +  "langchain-core>=1.8,<2",                                    
      12 +  "langchain-text-splitters>=1.1,<2",                          
      13 +  "langchain-ollama>=1.1,<2",                                  
      14 +  "pypdf>=7.1,<8",                                             
      15 +]                                                              
      16  
      17  [project.optional-dependencies]
      18  cloud = ["boto3>=1.35"]
  ⎿  … +1 file changed
     uv.lock
  ⎿  (timeout 10m)

  Ran 2 shell commands (ctrl+o to expand)

⏺ Contracts committed. Before spawning, I'll add the two small state-store
  helpers both backend agents need, so they don't block each other.

  Ran 1 shell command (ctrl+o to expand)

⏺ Bash(python3 - <<'EOF'
      def edit(path, old, new):…)
  ⎿     Building metis-retrieval @ file:///Users/nityanthmaramreddy/Downloa
     ds/Metis/services/retrieval
           Built metis-retrieval @ file:///Users/nityanthmaramreddy/Downloa
     … +6 lines (ctrl+o to expand)
  ⎿  Updated packages/contracts/src/metis_contracts/config.py +14
      111      job_max_attempts: int = field(default_factory=lambda: _env
           _int("METIS_JOB_MAX_ATTEMPTS", 5))
      112      job_lease_seconds: int = field(default_factory=lambda: _en
           v_int("METIS_JOB_LEASE_SECONDS", 300))
      113      outbox_batch: int = field(default_factory=lambda: _env_int
           ("METIS_OUTBOX_BATCH", 10))
      114 +    #: Worker processes: 1 interactive + (n-1) ingest. Minimum
          + 2.                                                           
      115 +    worker_processes: int = field(default_factory=lambda: _env
          +_int("METIS_WORKER_PROCESSES", 3))                            
      116 +    #: Global cap on leased ingest-lane events across all work
          +ers (backpressure).                                           
      117 +    ingest_max_in_flight: int = field(default_factory=lambda: 
          +_env_int("METIS_INGEST_MAX_IN_FLIGHT", 4))                    
      118 +    #: Longest a /v1/changes long-poll is held open.          
      119 +    changes_wait_seconds: int = field(default_factory=lambda: 
          +_env_int("METIS_CHANGES_WAIT_SECONDS", 25))                   
      120 +    #: Max file size the corpus ingests; larger files are skip
          +ped, not dead-lettered.                                       
      121 +    corpus_max_file_bytes: int = field(                       
      122 +        default_factory=lambda: _env_int("METIS_CORPUS_MAX_FIL
          +E_BYTES", 20 * 1024 * 1024)                                   
      123 +    )                                                         
      124 +    #: Share of retrieval candidates the corpus lane may take 
          +(percent).                                                    
      125 +    retrieval_corpus_share_pct: int = field(                  
      126 +        default_factory=lambda: _env_int("METIS_RETRIEVAL_CORP
          +US_SHARE", 50)                                                
      127 +    )                                                         
      128  
      129      daily_micro_usd: int = field(default_factory=lambda: _env_
           int("METIS_DAILY_MICRO_USD", 500_000))
      130      monthly_micro_usd: int = field(default_factory=lambda: _en
           v_int("METIS_MONTHLY_MICRO_USD", 10_000_000))
  ⎿  Updated services/state/src/metis_state/sqlite_store.py +17 -1
      64      "memory": ("project_id",),
      65      "proposal": ("state",),
      66      "project": ("state",),
      67 +    "connector": ("state", "trigger_at"),                      
      68  }
      69  
      70  
     ...
      201              "project_id": getattr(stored, "project_id", None),
      202              "local_date": _iso(getattr(stored, "local_date", N
           one)),
      203              "deadline_date": _iso(getattr(stored, "deadline_lo
           cal_date", None)),
      203 -            "trigger_at": _iso(getattr(stored, "trigger_at", N
          -one)),                                                        
      204 +            "trigger_at": _iso(getattr(stored, "trigger_at", N
          +one) or getattr(stored, "next_sync_at", None)),               
      205          }
      206          allowed = _QUERY_COLUMNS.get(kind, ())
      207          cols = {
     ...
      456      ) -> Page[SourceArtifact]:
      457          return self._page("source", user_id, "", [], cursor=cu
           rsor, limit=limit)
      458  
      459 +    def list_connectors(self, user_id: str, *, cursor: str | N
          +one = None, limit: int = 100) -> Page[Any]:                   
      460 +        return self._page("connector", user_id, "", [], cursor
          +=cursor, limit=limit)                                         
      461 +                                                              
      462 +    def due_connector_ids(self, *, now: datetime, limit: int =
          + 50) -> list[str]:                                            
      463 +        """Active connectors whose ``next_sync_at`` has passed
          +, as ``user_id/id``."""                                       
      464 +        with self._lock:                                      
      465 +            rows = self._conn.execute(                        
      466 +                """SELECT user_id, id FROM records            
      467 +                   WHERE kind='connector' AND tombstoned=0 AND
          + state='active'                                               
      468 +                     AND (trigger_at IS NULL OR trigger_at <= 
          +?)                                                            
      469 +                   ORDER BY trigger_at LIMIT ?""",            
      470 +                (now.isoformat(), limit),                     
      471 +            ).fetchall()                                      
      472 +        return [f"{r['user_id']}/{r['id']}" for r in rows]    
      473 +                                                              
      474      def list_reminders(
      475          self, user_id: str, *, state: ReminderState | None = N
           one, cursor: str | None = None, limit: int = 100
      476      ) -> Page[Reminder]:
  ⎿  (timeout 10m)

⏺ Now writing the shared interface contract all agents build against, then
  spawning wave 1.

⏺ Write(docs/agent-prompts/50-corpus-wave.md)
  ⎿  Wrote 98 lines to docs/agent-prompts/50-corpus-wave.md
       1 # Wave 50 — corpus, hybrid retrieval, queue lanes, change feed
       2 
       3 Spec: `docs/superpowers/specs/2026-11-21-metis-local-corpus-conn
         ectors-design.md`
       4 (decisions D1–D10, §14). Contracts are DONE and frozen for this 
         wave
       5 (commit 95bdd0b + 2e7ea30). Do not edit `packages/contracts/`. I
         f a contract is
       6 wrong, stop and report it in your final message.
       7 
     … +91 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Spawning wave 1: four agents in isolated worktrees (queue, retrieval, web,
  desktop).

⏺ 0 of 4 agents finished (ctrl+o to expand)
   ├ Agent A: queue lanes and workers
   │ ⎿  Failed
   ├ Agent B: corpus and hybrid retrieval
   │ ⎿  Failed
   ├ Agent D: web sync worker and UI
   │ ⎿  Failed
   └ Agent E: desktop Rust wiring
     ⎿  Failed

⏺ Worktree isolation refused — something odd about .git/logs/HEAD. Inspecting
  (not changing).
  Ran 2 shell commands (ctrl+o to expand)

⏺ Google Drive's upload cache hard-links .git/logs/HEAD, which blocks the
  isolation tool. I'm leaving that alone and creating plain git worktrees by
  hand instead. · summary

  Ran 1 shell command (ctrl+o to expand)

⏺ Worktrees ready. Spawning the four agents, each pinned to its own worktree.

⏺ Agent(Agent A: queue lanes and workers)
  ⎿  Not run · auto mode's check had no usable answer (ctrl+o to expand)

⏺ API Error: Your computer went to sleep mid-response. The response 
  above may be incomplete.

⏺ Agent(Agent A: queue lanes and workers)
  ⎿  Backgrounded agent (↓ to manage · ctrl+o to expand)

⏺ Agent(Agent B: corpus and hybrid retrieval)
  ⎿  Not run · auto mode's check had no usable answer (ctrl+o to expand)

⏺ API Error: Your computer went to sleep mid-response. The response 
  above may be incomplete.

⏺ API Error: Can't reach the API server — check your internet or DNS 
  (ENOTFOUND)

✻ Cooked for 50m 54s · done Saturday 10:06 PM

⏺ Agent "Agent A: queue lanes and workers" failed: Agent terminated early due to
  an API error: API Error: Can't reach the API server — check your internet or
  DNS (ENOTFOUND) (error type server_error)

⏺ Agent A died on network error (ENOTFOUND). Checking what it left before
  retrying.

  Ran 1 shell command (ctrl+o to expand)

⏺ Clean worktree, network back. Relaunching A and launching B, D, E.

⏺ Agent(Agent A: queue lanes and workers)
  ⎿  Not run · auto mode's check had no usable answer (ctrl+o to expand)

⏺ API Error: Your computer went to sleep mid-response. The response 
  above may be incomplete.

⏺ Classifier timing out on long prompts. Moving each brief to a file so the
  spawn calls are short.

  Made 1 scratchpad edit +20, ran 1 shell command (ctrl+o to expand)

⏺ API Error: Your computer went to sleep mid-response. The response 
  above may be incomplete.

  Made 3 scratchpad edits +65 (ctrl+o to expand)

⏺ 4 background agents launched (↓ to manage)
   ├ Agent A: queue lanes and workers
   ├ Agent B: corpus and hybrid retrieval
   ├ Agent D: web sync worker and UI
   └ Agent E: desktop Rust wiring

⏺ All four running. Meanwhile, writing wave-2 brief (API agent, runs after A+B
  merge).

  Made 1 scratchpad edit +19 (ctrl+o to expand)

⏺ I've updated the spec and started the build, which is split across four
  subagents running in parallel. None of them has reported back yet, so nothing
  is built or tested.

  Spec changes (committed):
  - Desktop: the spec now states the app is Rust (Tauri v2); the React build is
    only its view layer.
  - LangChain: retrieval uses LangChain's core retriever and embedding
    interfaces, its text splitter, and its Ollama embeddings. I left out
    langchain-classic and langchain-community, because they would bundle
    SQLAlchemy, LangSmith and hundreds of loaders into the desktop app. The step
    that merges keyword and semantic results is the same formula LangChain's
    EnsembleRetriever uses, written into our own retriever.
  - Hybrid search everywhere: every search and every planner lookup runs both
    semantic and keyword search. Keyword search also covers file names and
    paths, so "report" finds Q3-report.pdf like your find -iname example. Each
    result comes back as a citation: title, path, excerpt, character span, and
    which search found it.
  - Queue and dead-letter queue: the app already had a queue with retries and a
    dead-letter state, so I'm extending it rather than adding a new one. All
    data streams go through it. Work is split into two lanes: one for
    interactive tasks (captures, reminders, plans) and one for bulk ingestion
    (files, PDFs, email). The ingestion lane has a cap on how much runs at once,
    so a large import can't block reminders. Malformed files, PDFs and emails
    go straight to the dead-letter queue, and you can list and retry them.
  - Background sync off the UI thread: the Python workers re-sync each connected
    source on a timer. In the app, a dedicated Web Worker waits on a new change
    feed (/v1/changes), and the page only reloads what it's showing. This
    replaces the current 15-second refresh loop on the main page.

  Done in the main checkout (tests at baseline, 571 passing):
  - The shared data definitions: connectors, file/email/Drive sources, citation
    fields, queue stats, dead letters and the change feed.
  - Small state-store helpers for connectors and some new limits.
  - The LangChain and pypdf dependencies.

  Running now, each in its own git worktree under .worktrees/wave50-*:
  - A: the two queue lanes, the dead-letter list and retry, the change feed, and
    several Python worker processes.
  - B: reading local folders, parsing text and PDF files, splitting them into
    chunks, Ollama embeddings, the hybrid retrievers, and citations.
  - D: the web sync worker, a Sources screen (folders, queue counts, dead
    letters with Retry), and citations in search results.
  - E: the Rust side: settings passed to the Python workers, a check for whether
    Ollama is installed, a folder picker, "reveal in Finder/Explorer", and
    build targets for Windows and Linux.

  After A and B finish I'll merge them and start a fifth agent for the API
  endpoints (connectors, queue, dead letters, change feed, search). Its brief is
  ready.

  Two problems on the way:
  - The built-in way to give each agent an isolated copy of the repo refused to
    run. Google Drive's upload cache (~/Downloads/.tmp.driveupload) has a hard
    link to the repo's git log file. It's harmless and I didn't touch it, but
    you may want to exclude the Metis folder from Drive sync. I created the four
    worktrees by hand instead.
  - The first attempt at agent A died on a network error before making any
    changes; it's been relaunched.

  Gmail and Drive connectors aren't part of this round: they need a Google OAuth
  client and a decision on Google's app verification.

✻ Baked for 57m 23s · done Saturday 11:03 PM

› Message from general-purpose
  All five parts of the brief are built and committed as `9152cec` on branch
  `wave50-e-desktop`, in worktree
  `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-e-desktop`. All
  four checks pass on macOS arm64. I could not check the Windows or Linux
  builds: the only Rust target installed is `aarch64-apple-darwin`, so the
  cross-OS guards are unverified.

  **Commands registered (allowed for the main window only)**
  - `ollama_status() -> Result<OllamaStatus, String>`, returning `{ available:
  bool, models: Vec<String>, embed_model_present: bool }`.
    - It does a GET on `<endpoint>/api/tags` with a 1.5 s timeout, using the
  `ureq` client the crate already had.
    - If Ollama is down or the reply is malformed it returns `available:false`
  with an empty list. It returns `Err` only when the endpoint setting itself is
  invalid.
    - `nomic-embed-text` also matches `nomic-embed-text:latest`; a name with an
  explicit tag must match exactly.
  - `pick_folder() -> Option<String>`: native folder dialog via
  `tauri-plugin-dialog =2.8.4` (released 2026-10-28, needs tauri ^2.12). Returns
  `None` if the user cancels or the path is not UTF-8. The dialog is opened
  from Rust, so the webview gets no dialog or fs permissions.
  - `reveal_path(path: String) -> Result<(), String>`: the path must be absolute
  and exist. It is only shown, never opened or run:
    - macOS: `/usr/bin/open -R <path>`
    - Windows: `explorer.exe /select,"<path>"`, with the `\\?\` prefix stripped
  and paths containing `"` rejected.
    - Linux: `xdg-open` on the containing folder, since Linux has no portable
  "select".
    - I used `std::process` because `tauri-plugin-opener` was not in the
  dependency tree.

  **Environment passed to the backend**
  - To the API and the worker:
    - `METIS_EMBED_ENGINE`, default `auto`; must be `auto`, `fixture` or
  `local`.
    - `METIS_LOCAL_EMBED_MODEL`, default `nomic-embed-text`.
    - `METIS_LOCAL_LLM_ENDPOINT`, default `http://127.0.0.1:11434`. This is the
  name `LocalConfig.from_env` reads.
  - To the worker only: `METIS_WORKER_PROCESSES`, default 3, allowed range 1 to
  32.
  - The same variables are honoured if set in the app's own environment.
  - The endpoint must be `http://127.0.0.1:<port>` or `http://localhost:<port>`
  (a trailing `/` is allowed), and is passed on as `http://127.0.0.1:<port>`.
  Other hosts, https, paths, a missing port or port 0 are rejected.
  - An invalid value stops the local stack from starting and shows as a visible
  startup error, rather than silently falling back to a default.
  - Speech gets none of these.
  - `Config` now has a `local_ai: LocalAi` field. `Config::production` fills it
  with defaults, and `start_stack` overrides it with the app's environment.

  **Cross-OS**
  - `tauri.conf.json` `bundle.targets` is now
  `["app","dmg","msi","nsis","deb","appimage"]`.
  - The only stub is text insertion (`insertion.rs`). On non-macOS,
  `focused_app()` and `insert()` return "focused-app dictation is not supported
  on this OS yet (macOS only)". Its `Command` import is now macOS-only so other
  OSes build without warnings.
  - Audio, the HUD and the session code need no stubs. Audio uses `cpal`, which
  is portable; the HUD uses only cross-platform builder calls.
  - The microphone-silence message in `session.rs` now names macOS System
  Settings only on macOS.
  - The global hotkey (Super+Alt) registers through the plugin on every OS. If
  registration fails (likely on Linux Wayland), it already shows an honest
  error.

  **Checks (from `apps/desktop`)**
  - `cargo check --workspace --all-targets`: passed.
  - `cargo clippy --workspace --all-targets -- -D warnings`: no warnings.
  - `cargo fmt --all --check`: passed.
  - `cargo test --workspace`: all passed. That is 60 unit tests, 2
  `capture_e2e`, 1 `real_backend` and 10 `stack` tests.
    - New tests cover the environment defaults, overrides, rejection of
  non-loopback endpoints and bad values, which service gets which variables, tag
  parsing and model matching, Ollama being down, and `reveal_path` validation.
  - To build and test I symlinked the real backend from the main checkout into
  the worktree's `src-tauri/binaries/`; it is gitignored. I also ran `cargo
  build -p metis-health-sidecar`.

  **Files**
  - `apps/desktop/src-tauri/src/native.rs` (new): Ollama probe and reveal.
  - `src/stack.rs`: `LocalAi`, loopback check, `env_for` change, tests.
  - `src/lib.rs`: the three commands and the dialog plugin.
  - `src/insertion.rs`, `src/session.rs`.
  - `build.rs`, `capabilities/main.json`, `tauri.conf.json`, `Cargo.toml`,
  `tests/stack.rs`.
  - `permissions/autogenerated/{ollama_status,pick_folder,reveal_path}.toml`.
  - `apps/desktop/Cargo.lock`: outside my listed paths, but it changes whenever
  `Cargo.toml` does.

  Nothing in the contracts needs changing.

⏺ E done. Verifying its commit before merging.

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis && git log --oneline
      main..wave50-e-desktop; git diff --stat main...wave50-e-desktop | tail -3;
      git diff --name-onl…)
  ⎿  9152cec feat(desktop): local AI env, Ollama probe, folder picker, reve
     al, cross-OS bundles
      apps/desktop/src-tauri/tauri.conf.json             |   2 +-
     … +3 lines (ctrl+o to expand)
  ⎿  (another command ran in this repository at the same time; a change 
     made by either may show under either result)

⏺ Agent "Agent E: desktop Rust wiring" finished · 6m 17s

⏺ Clean: desktop-only paths, no binaries. Merging E into main.

⏺ Bash(git merge --ff-only -q wave50-e-desktop && git log --oneline -1)
  ⎿  9152cec feat(desktop): local AI env, Ollama probe, folder picker, reve
     al, cross-OS bundles
  ⎿  Updated apps/desktop/Cargo.lock +69
  ⎿  Updated apps/desktop/src-tauri/Cargo.toml +1
  ⎿  Updated apps/desktop/src-tauri/build.rs +3
  ⎿  Updated apps/desktop/src-tauri/capabilities/main.json +4 -1
  ⎿  Created
     apps/desktop/src-tauri/permissions/autogenerated/ollama_status.toml
     +11
  ⎿  … +9 files changed
     apps/desktop/src-tauri/permissions/autogenerated/pick_folder.toml
     apps/desktop/src-tauri/permissions/autogenerated/reveal_path.toml
     apps/desktop/src-tauri/src/insertion.rs
     +6 more
  ⎿  (a git step in this command can move the working tree, so these may be
     its changes, not edits) (ctrl+o to expand)

⏺ The desktop (Rust) agent finished, and I've merged its work into main
  (9152cec). The other three agents (queue, retrieval, web) are still running.

  What it added:
  - Three new desktop commands the web app can call:
    - ollama_status() checks whether Ollama is running and whether
      nomic-embed-text is installed.
    - pick_folder() opens the native folder picker.
    - reveal_path() shows a file in Finder, Explorer or the Linux file manager.
      It only accepts paths that are absolute and exist, and it never opens or
      runs the file.
  - Settings passed to the Python backend:
    - The embedding engine, the embedding model (default nomic-embed-text), the
      Ollama address and the number of worker processes (default 3).
    - The Ollama address must point to your own machine (127.0.0.1 or
      localhost). If a setting is invalid, the app refuses to start the backend
      and shows an error rather than quietly using a default.
  - Cross-OS: the app is now set to build installers for macOS, Windows and
    Linux. The only piece stubbed out is dictation typing into the focused app;
    outside macOS it says plainly that it isn't supported yet. Audio and the HUD
    needed no changes.

  All Rust checks pass on macOS: build, lint (no warnings), formatting and
  tests. I couldn't check Windows or Linux, because only the Mac build target is
  installed here, so the cross-OS changes are untested.

  Once the queue and retrieval agents finish, I'll merge them and start the API
  agent.

✻ Baked for 14s · done Saturday 11:09 PM
› Message from general-purpose
  ## Agent D (web): done

  Everything is on branch `wave50-d-web` in worktree
  `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-d-web`, in 4
  commits (`4baff56`, `77f03f3`, `62f16b3`, `5f8adf8`). I only touched
  `apps/web/`, staged files by explicit path, and did not edit
  `contracts/metis.ts`.

  **Verification:** `pnpm --filter @metis/web typecheck` is clean. `pnpm
  --filter @metis/web test` passes 129 tests in 14 files; 22 of them are new
  (syncCore 10, api 3, evidence 3, sources 6). `pnpm --filter @metis/web build`
  succeeds and emits the worker as its own chunk (`sync.worker-*.js`). I did not
  run the UI in a browser, because the Agent C endpoints don't exist yet.

  ### Files (all under `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wav
  e50-d-web/apps/web/src/`)
  - **`lib/syncCore.ts`** (new): the sync loop as pure logic.
    - Long-polls `/v1/changes?after=<cursor>&wait=25` with the bearer and keeps
  the cursor in memory.
    - On failure it posts `offline` with a retry delay and backs off 1s, 2s, 4s…
  up to 30s.
    - On a 401 it posts `unauthorized` and waits for a `{type:'token'}` message
  instead of retrying.
    - `reset` is passed straight through to the page.
    - It sends `{type:'changes', kinds, ids, queue, reset}` at most once per
  300ms. A queue-count change on its own also counts as news.
    - **`lib/syncCore.test.ts`** covers it.
  - **`lib/sync.worker.ts`** (new): the dedicated module worker that runs the
  loop. The base URL and token arrive only by postMessage.
  - **`lib/useSync.ts`** (new):
    - `SyncProvider` runs one worker per session and is mounted in `App.tsx`
  around `SessionProvider`.
    - `useSync()` exposes `{status, retryInMs, queue, subscribe}`.
    - `useChanges(kinds, cb)` is the per-screen hook.
    - A new auth token is forwarded to the worker.
  - **`app/session.tsx`**: the 15s `setInterval` is gone. There is one initial
  load, then a refresh on `notification` (and `preferences`) changes.
  - **Screens refetch only on their kinds:**
    - Tasks: task, event
    - Inbox: task, event, proposal, source
    - Today: task, event, plan, reminder
    - Projects: project
    - Sources: connector, source
  - **`lib/api.ts`**: typed client calls for every endpoint in the table, plus
  `syncCredentials()` (gives the worker an absolute base URL and bearer) and
  `subscribeAuthToken()`. Tests are in **`lib/api.test.ts`**.
  - **`lib/desktop.ts`**: added `desktopBearer()` and `desktopCommand(cmd,
  args)`. Outside the desktop app, or if the call fails, it returns
  `{ok:false}`, which is how the optional commands are feature-detected.
  - **`lib/sources.ts`** (new, tested in **`lib/sources.test.ts`**): parses the
  folder form (paths one per line and must be absolute; globs split on commas or
  lines) and decides the status words. It never shows "Up to date" before a
  first sync or while the ingest queue has work.
  - **`screens/SourcesScreen.tsx`** (new):
    - Lists connectors with title, kind, status, paths, item count, last sync
  and last error.
    - Add folder, with a "Choose folder…" button that calls `pick_folder` and
  falls back to typing the path.
    - Pause/resume sends `expected_revision`; a 409 refetches and says so.
    - "Sync now" says the sync is queued, not that it ran. Remove asks for
  confirmation on the page.
    - A queue table per lane and a dead-letter list with Retry.
    - Status messages: "Indexing…", "Keyword search only" (from `ollama_status`)
  and "Needs re-auth". It also shows when live updates are offline or refused.
    - Reachable from a new "Sources" rail entry (`App.tsx`, folder icon in
  `components/Icon.tsx`) and from a Settings header button.
  - **`lib/evidence.ts`** (tested in **`lib/evidence.test.ts`**),
  **`components/EvidenceLink.tsx`**, **`screens/SearchScreen.tsx`**:
    - Search results show an origin chip (Memory/File/Email/Drive), title, path
  or URI, excerpt and Semantic/Keyword badges, plus `degraded_reason`.
    - For a file, "Show file" calls `reveal_path` and falls back to copying the
  path. Email and Drive results get an "Open original" link (http/https only).
    - Plan evidence labels now read "File · title". File, email and Drive
  citations open a dialog with the citation details instead of loading a capture
  transcript.

  ### Contract problem (not fixed, needs the coordinator)
  The generated TypeScript has `Evidence.matched?: "semantic" | "keyword"[];`.
  That reads as `"semantic" | ("keyword"[])`, so the type is wrong. It should be
  `("semantic" | "keyword")[]`, which means the generator needs parentheses
  around union array items. I worked around it in `matchedOf()`, which treats
  whatever arrives as an array.

  ### Assumptions about the API
  - **Change kinds** are the state-store record kinds (`task`, `event`,
  `proposal`, `source`, `notification`, `connector`, …).
  - **Baseline:** on startup the worker calls `/v1/changes?after=0&wait=0` and
  keeps calling with `wait=0` until a response comes back with no changes or the
  cursor stops moving. That history is skipped, not replayed, because the page
  already did its initial load. If the server caps a page at 500 changes, a
  large history means about one quick request per 500 changes at startup. An
  endpoint that returns just the current max cursor would remove this.
  - **`reset: true`** means the server could not continue from our cursor; the
  worker then uses the cursor the server returns.
  - **Queue counts** come from `ChangesResponse.queue`, with a single `GET
  /v1/queue` when Sources opens. Dead letters are refetched when the dead-letter
  total changes, since they are not in the change feed.
  - **Connectors:** `last_sync_at` is set when a sync finishes, and `item_count`
  counts indexed items. `GET /v1/connectors?cursor=` is used for paging. The
  sync and create calls send a fresh `Idempotency-Key` each time.
  - **Citation URIs** for files are either `file://` URIs or plain absolute
  paths; email and Drive URIs are http(s) links.
  - **Desktop commands:** `pick_folder` is called with no arguments,
  `reveal_path` with `{ path }`, and `ollama_status` should return `{available,
  models, embed_model_present}`. I didn't add a Sources component test because
  the project has no component-test setup.

⏺ Web agent done. It found a real codegen bug ("semantic" | "keyword"[]). Fixing
  generator — contracts are mine.

  Searched for 1 pattern (ctrl+o to expand)

⏺ Agent "Agent D: web sync worker and UI" finished · 7m 51s

  Ran 1 shell command (ctrl+o to expand)

⏺ Bash(python3 - <<'EOF'
      p="packages/contracts/src/metis_contracts/generate.py"…)
  ⎿   .gitignore                                         | 2 ++
      apps/web/src/contracts/metis.ts                    | 2 +-
      packages/contracts/generated/metis.ts              | 2 +-
     … +5 lines (ctrl+o to expand)
  ⎿  Updated apps/web/src/contracts/metis.ts +1 -1
      663    uri?: string | null;
      664    start_char?: number | null;
      665    end_char?: number | null;
      666 -  matched?: "semantic" | "keyword"[];                         
      666 +  matched?: ("semantic" | "keyword")[];                       
      667  }
      668  
      669  /** Owner-scoped export state. ``download_url`` exists only on
           ce the archive */
  ⎿  Updated packages/contracts/generated/metis.ts +1 -1
      663    uri?: string | null;
      664    start_char?: number | null;
      665    end_char?: number | null;
      666 -  matched?: "semantic" | "keyword"[];                         
      666 +  matched?: ("semantic" | "keyword")[];                       
      667  }
      668  
      669  /** Owner-scoped export state. ``download_url`` exists only on
           ce the archive */
  ⎿  Updated packages/contracts/src/metis_contracts/generate.py +2 -1
      124      t = schema.get("type")
      125      if t == "array":
      126          inner = _ts_type(schema.get("items", {}), model_names)
            if schema.get("items") else "unknown"
      127 -        return f"{inner}[]"                                   
      127 +        # A union item needs parentheses: `"a" | "b"[]` would 
          +mean `"a" | ("b"[])`.                                         
      128 +        return f"({inner})[]" if " | " in inner else f"{inner}
          +[]"                                                           
      129      if t == "object":
      130          extra = schema.get("additionalProperties")
      131          if isinstance(extra, dict):

⏺ Merging the web branch and re-running web checks on main.

⏺ Bash(git diff --name-only main...wave50-d-web | grep -v '^apps/web/'; git
      merge -q --no-edit -m "merge: wave50 web sync worker, Sources screen,
      citations…)
  ⎿  0a9b544 merge: wave50 web sync worker, Sources screen, citations
     > tsc --noEmit
     … +2 lines (ctrl+o to expand)
  ⎿  Updated apps/web/src/app/App.tsx +12 -5
  ⎿  Updated apps/web/src/app/session.tsx +5 -6
  ⎿  Updated apps/web/src/components/EvidenceLink.tsx +83 -3
  ⎿  Updated apps/web/src/components/Icon.tsx +1
  ⎿  Updated apps/web/src/lib/api.test.ts +48
  ⎿  … +17 files changed
     apps/web/src/lib/api.ts
     apps/web/src/lib/desktop.ts
     apps/web/src/lib/evidence.test.ts
     +14 more
  ⎿  (a git step in this command can move the working tree, so these may be
     its changes, not edits) (ctrl+o to expand)
  ⎿  (timeout 5m)

⏺ Web merged: 129/129 tests, typecheck clean. Folding the web agent's API notes
  into the API brief.

  Ran 1 shell command (ctrl+o to expand)

⏺ The web agent finished and its work is merged into main. The web tests pass
  (129 of 129) and typechecking is clean.

  What it built:
  - Background sync: the 15-second refresh loop on the main page is gone. A
    dedicated Web Worker now waits on /v1/changes. If the connection drops it
    retries with growing gaps up to 30 seconds, and on an expired login it waits
    for a new token instead of retrying. Each screen reloads only the data it
    shows.
  - Sources screen, reachable from a new sidebar entry and from Settings:
    - Add a folder with the native folder picker, or by typing the path.
    - Pause, resume and remove a source; "Sync now" says the sync is queued, not
      that it ran.
    - Per-lane queue counts, and a dead-letter list with a Retry button.
    - Honest status messages such as "Indexing…" and "Keyword search only".
  - Citations in search: each result shows whether it came from Memory, a File,
    Email or Drive, plus its title, path, excerpt and a Semantic or Keyword
    badge. "Show file" opens Finder or Explorer at the file. Plan evidence is
    labelled the same way.

  Fixed along the way: it found a bug in the code that generates the TypeScript
  types. It produced the wrong type for the "which search matched" list. I fixed
  it in the generator (bd59380).

  The web screens haven't been run against a real backend yet, because the API
  endpoints don't exist. The API agent will build those once the queue and
  retrieval agents finish; I've added the web agent's notes about expected API
  behaviour to its brief. The queue and retrieval agents are still running.

✻ Worked for 48s · done Saturday 11:11 PM

› Message from general-purpose
  Agent A (queue) is finished on branch `wave50-a-queue` in its worktree, in two
  commits: 35575b2 (state) and 021c67b (pipeline, worker and backend).
  Everything is implemented and the full suite passes: 580 passed, 1 skipped.
  That is the 571 baseline plus 9 new tests. ruff check, ruff format --check and
  mypy on services/state and services/pipeline are clean.

  **What was built**
  - **Lanes and backpressure:** `claim_outbox` takes `lane` and `max_in_flight`
  as specified. The count of unexpired leases in that lane and the claim happen
  in the same `BEGIN IMMEDIATE`, so the cap holds across all workers. Ordering
  by `available_at` is kept.
  - **Queue reads:** `queue_stats(user_id=None)` reports per-lane counts, and an
  expired lease counts as pending. `list_dead_letters(limit, *, user_id=None)`
  and `requeue_outbox(user_id, event_id) -> bool` are added; requeue works only
  on the owner's dead letters.
  - **Change feed:** a `changes` table and its `(user_id, seq)` index are in
  `schema.sql`; existing databases get the table on open. A row is appended in
  `_put_locked` and in `tombstone`, in the same transaction; `tombstone` now
  runs in its own `BEGIN IMMEDIATE` and records nothing when no row matched.
  Kinds outbox, budget_reservation and usage_day are skipped.
  `list_changes(user_id, *, after, limit=500)` returns the changes and a cursor;
  with no changes, the cursor is the current max. `max_change_seq` is added.
  - **Dispatcher:** `lane_of(event_type)` and `OutboxDispatcher(...,
  lane="all")` are added. The ingest lane passes
  `settings().limits.ingest_max_in_flight`.
  - **Multi-process worker:** `local_worker` takes `--processes N`, defaulting
  to `worker_processes`.
    - N=1 is today's single all-lanes loop, which also fires connector syncs
  now.
    - N≥2 spawns child 0 (interactive lane plus reminders, daily plans and
  `_fire_due_syncs`), then children 1..N-1 on the ingest lane.
    - Each child opens its own connection and stops on the shared stop event, on
  SIGTERM, or when its parent dies.
    - The parent emits the readiness record exactly once, after all children are
  ready, in the same shape as before. It forwards stop, joins the children and
  terminates any that hang. If a child dies, the parent exits 1.
  - **`metis_backend.py`:** calls `multiprocessing.freeze_support()` first, and
  stdin EOF now calls `local_worker.request_stop()`, which stops every child. I
  removed the old monkeypatching of `local_worker`. `build.sh` needs no change.

  **Deviations from the interface doc**
  - **Contract gap:** the `StateStore` protocol in `ports.py` still has the old
  `claim_outbox` signature and none of the new methods. To avoid editing the
  frozen contracts, the dispatcher passes the new arguments only when `lane !=
  "all"`, through an `Any` handle. Agent C will need the same workaround or a
  coordinator update to the protocol.
  - **Startup race:** concurrent workers opening a fresh database crashed with
  "database is locked", reproduced about 4 times in 12 runs. It happens in the
  state store and also in retrieval's `memory_store.py`, which is Agent B's
  path. I fixed it two ways in my paths: child 0 starts and creates the
  databases before its siblings, and `SqliteStateStore` retries the schema
  script briefly when it sees "locked". After that, 15 of 15 runs were clean.
  The API and worker starting at the same time could hit the same race, so
  retrieval may want the same retry.
  - `--once` always runs in a single process, whatever N is.
  - `_fire_due_syncs` imports `metis_retrieval.connectors.enqueue_due_syncs`
  lazily and returns 0 if it is missing. If it raises, the error is logged and 0
  is returned, so a sync bug cannot stop reminders.
  - Each process's `worker_id` now ends in `:<pid>:<lane>`, because the children
  were otherwise getting identical IDs.
  - `dynamo_store.py` is untouched, since typing did not require stubs.

  **Deferred**
  - The PyInstaller frozen build was not run, so spawn under a onefile binary
  has not been tested; `freeze_support()` is in place.
  - `apps/desktop/python-backend/tests` is not in pytest's `testpaths` (this was
  already the case). Its 2 tests pass when run with
  `PYTHONPATH=apps/desktop/python-backend`.

  **Files changed**
  - `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-a-queue/service
  s/state/src/metis_state/schema.sql`
  - `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-a-queue/service
  s/state/src/metis_state/sqlite_store.py`
  - `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-a-queue/service
  s/state/tests/test_queue_and_changes.py` (new, 6 tests)
  - `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-a-queue/service
  s/pipeline/src/metis_pipeline/dispatcher.py`
  - `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-a-queue/service
  s/pipeline/src/metis_pipeline/local_worker.py`
  - `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-a-queue/service
  s/pipeline/tests/test_worker_lanes.py` (new, 3 tests, including a real
  2-process start and SIGTERM stop)
  - `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-a-queue/apps/de
  sktop/python-backend/metis_backend.py`

⏺ Queue agent done (580 pass). Merging, then I'll update the StateStore protocol
  in contracts (my job) and pass the DB-lock race note to the retrieval agent.
  ⎿  Message queued for delivery to ada63d5210d4f14ef at its next tool 
     round. Sending this moved nothing it is running to the background just
     now, so the message waits for that round unless something moves 
     sooner. If it finishes first, it is resumed with the message and 
     notifies again, so wait for that notification before resending.

⏺ Bash(git diff --name-only main...wave50-a-queue; git merge -q --no-edit -m
      "merge: wave50 queue lanes, DLQ, change feed, multi-process workers…)
  ⎿  apps/desktop/python-backend/metis_backend.py
     services/pipeline/src/metis_pipeline/dispatcher.py
     services/pipeline/src/metis_pipeline/local_worker.py
     … +11 lines (ctrl+o to expand)
  ⎿  Updated apps/desktop/python-backend/metis_backend.py +8 -17
  ⎿  Updated services/pipeline/src/metis_pipeline/dispatcher.py +29 -5
  ⎿  Updated services/pipeline/src/metis_pipeline/local_worker.py +202 -30
  ⎿  Created services/pipeline/tests/test_worker_lanes.py +87
  ⎿  Updated services/state/src/metis_state/schema.sql +14
  ⎿  … +2 files changed
     services/state/src/metis_state/sqlite_store.py
     services/state/tests/test_queue_and_changes.py
  ⎿  (a git step in this command can move the working tree, so these may be
     its changes, not edits) (ctrl+o to expand)

⏺ Agent "Agent A: queue lanes and workers" finished · 11m 39s

  Ran 2 shell commands (ctrl+o to expand)

⏺ Bash(python3 - <<'EOF'
      p="packages/contracts/src/metis_contracts/ports.py"…)
  ⎿  Success: no issues found in 74 source files
  ⎿  Updated packages/contracts/src/metis_contracts/ports.py +28 -3
      22  from datetime import date, datetime
      23  from typing import Any, Literal, Protocol, runtime_checkable
      24  
      25 +from .api import Change, QueueStats                            
      26  from .common import Page
      27  from .entities import (
      28      AvailabilityBlock,
     ...
      162          separate resumable job."""
      163          ...
      164  
      164 -    def list_dead_letters(self, limit: int = 50) -> list[Outbo
          -xEvent]:                                                      
      165 -        """Operator read path for events that exhausted their 
          -retries."""                                                   
      165 +    def list_dead_letters(self, limit: int = 50, *, user_id: s
          +tr | None = None) -> list[OutboxEvent]:                       
      166 +        """Events that exhausted their retries or failed perma
          +nently (malformed input)."""                                  
      167          ...
      168  
      169 +    def requeue_outbox(self, user_id: str, event_id: str) -> b
          +ool:                                                          
      170 +        """Move one of the owner's dead letters back to pendin
          +g with attempts reset."""                                     
      171 +        ...                                                   
      172 +                                                              
      173 +    def queue_stats(self, user_id: str | None = None) -> Queue
          +Stats: ...                                                    
      174 +                                                              
      175 +    def list_changes(self, user_id: str, *, after: int, limit:
          + int = 500) -> tuple[list[Change], int]:                      
      176 +        """Change feed rows with ``seq > after`` and the curso
          +r to resume from."""                                          
      177 +        ...                                                   
      178 +                                                              
      179 +    def max_change_seq(self, user_id: str) -> int: ...        
      180 +                                                              
      181 +    def list_connectors(self, user_id: str, *, cursor: str | N
          +one = None, limit: int = 100) -> Page[Any]: ...               
      182 +                                                              
      183 +    def due_connector_ids(self, *, now: datetime, limit: int =
          + 50) -> list[str]: ...                                        
      184 +                                                              
      185      def get_preferences(self, user_id: str) -> Preferences: ..
           .
      186  
      187      def get_planning_epoch(self, user_id: str) -> int: ...
      188  
      189      # --- jobs and outbox ------------------------------------
           ---------------
      173 -    def claim_outbox(self, *, owner: str, limit: int, lease_se
          -conds: int) -> list[OutboxEvent]: ...                         
      190 +    def claim_outbox(                                         
      191 +        self,                                                 
     … +10 lines (ctrl+o to expand)
  ⎿  (timeout 5m)

⏺ mypy clean. Removing the queue agent's Any workaround in the dispatcher now
  that the protocol is updated.

  Ran 2 shell commands (ctrl+o to expand)

⏺ Bash(python3 - <<'EOF'
      p="services/pipeline/src/metis_pipeline/dispatcher.py"…)
  ⎿  Success: no issues found in 74 source files
      M .gitignore
      M packages/contracts/src/metis_contracts/ports.py
     … +2 lines (ctrl+o to expand)
  ⎿  Updated services/pipeline/src/metis_pipeline/dispatcher.py +2 -3
      209                  owner=self._context.worker_id, limit=batch, le
           ase_seconds=limits.job_lease_seconds
      210              )
      211          else:
      212 -            # The StateStore port predates lanes; only the SQL
          -ite store has them.                                           
      213 -            state: Any = self._context.state_store            
      214 -            events = state.claim_outbox(                      
      212 +            # Lane args only when needed, so simple test fakes
          + keep the old signature.                                      
      213 +            events = self._context.state_store.claim_outbox(  
      214                  owner=self._context.worker_id,
      215                  limit=batch,
      216                  lease_seconds=limits.job_lease_seconds,
  ⎿  (timeout 10m)

  Ran 1 shell command (ctrl+o to expand)

⏺ The queue agent finished, and its work is merged into main. The full Python
  suite passes: 580 tests, the 571 from before plus 9 new.

  What it built:
  - Two lanes, with a cap on bulk work: one lane for interactive work, one for
    bulk ingestion. The cap on how many ingestion jobs run at once is checked
    and enforced in the same step, so it holds across all worker processes.
  - Dead-letter queue: you can list dead letters and send them back to the
    queue, but only your own. Malformed files still go there straight away.
  - Change feed: every save and delete writes a change entry in the same step,
    which is what the web sync worker reads.
  - Multiple Python workers:
    - With the default of 3 processes, one handles interactive work (captures,
      reminders, daily plans, triggering connector syncs) and two handle
      ingestion.
    - Workers stop if the app quits or crashes.
    - The desktop app still sees exactly one "worker ready" signal, unchanged.

  Two fixes along the way:
  - The agent found a startup race: several workers opening a new database at
    the same moment could fail with "database is locked". It fixed this for the
    main database. I've told the retrieval agent, which is still running, to
    apply the same fix to its search-index database.
  - The contract that every state store must follow (StateStore) didn't include
    the new queue methods yet. I added them, which let me remove a workaround in
    the dispatcher (b6a4eb3). Typechecking and the full suite still pass.

  Not tested yet: worker processes inside the packaged desktop backend, because
  I haven't run that slow build. The required setup call for packaged builds is
  in place.

  Of the first four agents, only retrieval is still running. When it finishes,
  I'll merge it and start the API agent, which builds the endpoints the web and
  queue work need.

✻ Brewed for 2m 1s · done Saturday 11:16 PM

› Message from general-purpose
  Agent B (retrieval) is done. Everything is in worktree
  `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-b-retrieval`,
  branch `wave50-b-retrieval`, as 6 commits from 92xx… through the last
  `fix(retrieval): busy timeout and locked-retry…`. I only touched
  `services/retrieval/`. Contracts are unchanged.

  **Verification**
  - Retrieval + planner tests: 63 passed.
  - Full suite: 584 passed, 1 skipped (baseline was 571 + 1 skipped; the 13 new
  tests account for the difference).
  - `ruff check`, `ruff format --check` and `mypy services/retrieval` are all
  clean.

  **Coordinator's lock note: fixed.** `SqliteMemoryStore` now opens with
  `sqlite3.connect(..., timeout=10)` as a busy timeout. A new `run_schema()` in
  `corpus_store.py` retries `executescript` on "locked" with short backoff, and
  both the memory schema and the corpus schema use it. I checked it with a
  scratch script that has 6 processes open a fresh `memory.sqlite` at once, 10
  times over: 0 failures.

  **Files (under `services/retrieval/src/metis_retrieval/`)**
  - `embedder.py`
    - New `METIS_EMBED_ENGINE` (`auto|fixture|local`). `auto` picks local when
  `METIS_LOCAL_EMBED_MODEL` is set, otherwise it behaves as before.
    - All embedders are now LangChain `Embeddings`. `LocalEmbedder` wraps
  `OllamaEmbeddings` on the loopback endpoint with `trust_env=False`. It keeps
  the dimension checks, `model_id` and `dimensions`, and reports failures as
  `PROVIDER_UNAVAILABLE`.
    - `semantic_available()` returns false for the fixture, so the fixture is
  reported honestly.
  - `parsers.py`: `parse_file` handles text/md/code/csv (utf-8, latin-1
  fallback, binary rejected) and PDF via pypdf. No text layer gives
  `UNSUPPORTED_MEDIA`; corrupt or encrypted gives `VALIDATION_FAILED`.
  - `ingestion.py`
    - `chunk_text` uses `RecursiveCharacterTextSplitter` (2000 chars, 240
  overlap, `add_start_index`). Each chunk keeps its exact start/end, `span_ref`
  is `chunk:s-e`, and the embedded text gets a `title › path` context line.
    - `memory_content_for(item, source)` is new, and `memories_from_proposal`
  now uses it.
  - `corpus_store.py`: the `chunks` table plus FTS5 over title, uri and content
  (bm25 weights title > uri > content), with prefix matching. Vectors are
  float32 `array('f')`.
    - It reuses vectors by hash + model, including the unchanged chunks of a
  re-ingested file.
    - It has delete-by-source and an exact cosine scan marked `# ponytail:` as
  the sqlite-vec upgrade point.
    - It is reached as `SqliteMemoryStore(...).corpus`, so the existing
  `RetrievalService` constructors in api, planner and the worker pick it up
  without changes.
  - `retrievers.py`
    - `KeywordRetriever`, `SemanticRetriever` and `HybridRetriever` are all
  `BaseRetriever`.
    - Fusion is weighted RRF with c=60, and results carry `matched`.
    - It has a memory lane and a corpus lane.
    - LangSmith tracing env vars are forced to false on import, with a comment
  explaining why.
  - `service.py`
    - `search(...)` returns `tuple[list[SearchResult], str | None]` with
  `Evidence` citations (origin, chunk_id, title, uri, start/end, excerpt ≤2000,
  matched).
    - The corpus share is capped by `retrieval_corpus_share_pct` when the memory
  lane is part of the query.
    - Owner checks run at both query and hydration. Corpus hits are dropped if
  the source is missing, tombstoned or at a different version.
    - With fixture or failing embeddings it runs keyword-only and returns a
  reason starting "keyword search only…".
    - `retrieve_supporting_memories` keeps its signature but is now hybrid and
  returns memories only.
    - `evidence_for_task` now includes corpus evidence labelled by origin,
  within the same budget.
  - `connectors.py`
    - `DEFAULT_EXCLUDES` / `SECRET_EXCLUDES`. Secrets always win over the user's
  globs, and symlinks are never followed.
    - Versions are `mtime_ns:size`, with a sha256 only when that changes; a
  touched but identical file is not re-ingested.
    - Upserts a FILE source and raises the ingest event, or tombstones the
  source and raises the purge event.
    - At most 500 events per pass, then it re-enqueues a continuation.
    - Exposes `enqueue_sync`, `enqueue_due_syncs`, `remove_connector` and
  `sync_connector`. A bad root sets status=error with an honest `last_error`.
  - `handlers.py`: handlers for the three ingest events. The source-ingest flow
  is stale → `REVISION_CONFLICT`, then parse → chunk → index → embed, then READY
  with `chunk_count`. A parse failure marks the source FAILED and re-raises so
  it dead-letters.
  - `memory_store.py`, `__init__.py`: wiring and exports.
  - Tests: `services/retrieval/tests/test_corpus.py`.

  **Deviations**
  - `retrieve_supporting_memories` returns a reason only when the index is
  unavailable or fails. Keyword-only mode is reported by `search()` alone, so a
  desktop fixture setup still gives planner evidence.
  - When `search()` gets a `project_id`, it searches memories only, because
  corpus chunks have no project.
  - The fixture embedder now means keyword-only everywhere, so the eval
  harness's "dense" arm returns nothing under the fixture. That is the honest
  result.
  - A file source's extracted text is not stored on the `SourceArtifact`. It
  lives only in the chunks, to keep large bodies out of the state DB and its
  revisions table.
  - `enqueue_sync` also pushes `next_sync_at` forward in the same transaction,
  so the worker tick doesn't enqueue the same connector again.
  - `sync_connector` skips paused and needs_auth connectors only, so a manual
  sync can recover a connector in error. Due-scheduling still covers active
  connectors only.
  - The retrievers module sets `LANGSMITH_TRACING` / `LANGCHAIN_TRACING*` to
  false for the whole process.

  **Deferred**
  - sqlite-vec.
  - Heading-path context and email/DOCX parsers.
  - Re-embedding the corpus when the model changes; until then it only happens
  on the next ingest of each file.
  - If Ollama stays down until an ingest event dead-letters, that source stays
  PROCESSING, though its chunks are still keyword-searchable.
  - An origin index for finding a connector's sources; it currently pages
  `list_sources` (`# ponytail:`).
  - The planner prompt doesn't yet render `Evidence.origin`; that lives in the
  planner package.
  - `/v1/search` in `routes/projects.py` still calls
  `retrieve_supporting_memories`. Agent C should switch it to `search()`.

  **Contract problems:** none blocking. One thing to know: `tx` has no tombstone
  method, so sources are put with lifecycle TOMBSTONED plus the purge event in
  one transaction, and `state.tombstone()` is called after commit. Hydration
  checks both, so this is safe.

⏺ Retrieval agent done (584 pass). Merging, running the full suite on combined
  main, then launching the API agent.

  Ran 1 shell command · 1 failed (ctrl+o to expand)

⏺ Agent "Agent B: corpus and hybrid retrieval" finished · 14m 7s

⏺ Ruff hits are only in old pre-existing .worktrees/agent-* copies, not main
  code. Re-checking without them, then launching the API agent.

  Ran 1 shell command (ctrl+o to expand)

⏺ Only nit: ruff wants two blank lines before the def in my spec's code block.
  Fixing, then spawning API agent.

⏺ Bash(f=docs/superpowers/specs/2026-11-21-metis-local-corpus-connectors-design.
      md && uv run --frozen ruff format -q "$f" && uv run --frozen ruff format
      --check -q --e…)
  ⎿  ok
  ⎿  Updated docs/superpowers/specs/2026-11-21-metis-local-corpus-connector
     s-design.md +1
      124  
      125  GMAIL_BACKFILL = timedelta(days=30)
      126  
      127 +                                                              
      128  def gmail_backfill_query(clock: Clock) -> str:
      129      # clock.now() is timezone-aware UTC (datetime.now(UTC) in 
           production,
      130      # a controllable clock in tests/e2e). Gmail's after: accep
           ts epoch seconds,
  Ran 1 shell command (ctrl+o to expand)

⏺ Agent(Agent C: API endpoints)
  ⎿  Backgrounded agent (↓ to manage · ctrl+o to expand)

⏺ The retrieval agent finished, and its work is merged into main. With all four
  first-wave agents merged, the full Python suite passes (593 tests), as do
  typechecking, lint and formatting.

  What it built:
  - Hybrid search: keyword and semantic search run side by side as LangChain
    retrievers, and their results are merged. This covers both your own memories
    and imported files. Each result comes back as a citation: where it came
    from, title, path, excerpt, character span, and which search found it.
  - Search by file name: keyword search covers file names and paths as well as
    contents, so "report" finds Q3-report.pdf even when the text inside never
    says "report".
  - Local folder sync:
    - Secret folders and files are always skipped, whatever the user's own
      include or exclude patterns say. These include .ssh, .env, keys and .git.
    - A file that was only touched, with its contents unchanged, isn't
      re-imported.
    - Deleted files are removed from the index.
    - Each sync pass is capped at 500 files and continues in the next pass.
  - File parsing: text, markdown, code, CSV and PDF. A corrupt, encrypted or
    text-less PDF goes straight to the dead-letter queue.
  - Embeddings: these use Ollama. Without Ollama, search runs keyword-only and
    says so.
  - Privacy: LangSmith tracing is forced off, so file contents never leave your
    machine.
  - Database lock fix: it also fixed the "database is locked" startup race for
    its own databases. Six processes opening a fresh database at once, ten times
    over, gave no failures.

  Still to do (deferred):
  - The faster vector index (sqlite-vec).
  - Email and Word document parsers.
  - Re-indexing everything automatically after switching embedding model.
  - Having the planner show where each piece of evidence came from.
  - If Ollama stays down long enough for a file to land in the dead-letter
    queue, that file shows "processing" indefinitely. Keyword search still finds
    it.

  The last agent is now running. It builds the API endpoints the rest needs:
  connectors, the queue and dead-letter list, the change feed, and switching
  /v1/search to the new hybrid search. Until it finishes, the Sources screen and
  live sync have no backend to talk to.

✻ Baked for 1m 47s · done Saturday 11:19 PM

› Message from general-purpose
  Agent C is finished. Every endpoint in the interface table is built, tested
  and committed on branch `wave50-c-api` in
  `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-c-api`. I worked
  only in `services/api/` and did not touch contracts.

  **Verification:** the full suite (`-m "not integration and not cloud"`) gives
  603 passed, 1 skipped, 2 deselected. The API package alone is 144 passed: 134
  existing plus 10 new. `ruff check`, `ruff format --check` and `mypy
  services/api` are all clean.

  **Commits**
  - `ace1b61` feat(api): connectors, queue, dead letters, change feed, hybrid
  search
  - `a1f542a` test(api): connectors, dead letters, change feed long-poll, file
  citations

  **Files**
  - `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-c-api/services/
  api/src/metis_api/routes/connectors.py` (new)
    - **Connector create:** requires an Idempotency-Key. Each path must be
  absolute, an existing directory, and not inside a `DEFAULT_EXCLUDES` folder
  (checked after resolving symlinks). Otherwise it returns 422. Paths are stored
  resolved and deduplicated. The connector (with `next_sync_at` set) and its
  `connector.sync_requested` event with `full: true` commit in one transaction.
    - **Connector list, patch, delete:** list pages with a cursor. Patch returns
  409 on a stale revision and checks paths again when config changes. Delete
  calls `remove_connector` and returns 204.
    - **Sync now:** requires an Idempotency-Key, calls `enqueue_sync`, and
  returns 202 with `job_id` set to the outbox event id.
    - **Queue:** `GET /v1/queue` and the dead-letter list are owner-scoped. Each
  dead letter gets its lane from `lane_of`, and its title from the source's
  origin when the target is a source. Retry returns 202 `{}`, or 404 when the
  event is not the caller's or is no longer dead.
    - **Change feed:** `GET /v1/changes` is `async def`. Store calls run in a
  threadpool and it waits with `asyncio.sleep(0.5)`, checking `max_change_seq`
  each time. The wait is capped at `min(wait, changes_wait_seconds)`.
      - With `after` omitted it returns `{cursor: current max, changes: []}`
  straight away.
      - With `after` above the current max it returns `reset: true` and the
  current max as the cursor.
      - `queue` stats are on every response.
  - `routes/__init__.py` and `main.py`: register the new router.
  - `routes/projects.py`: `/v1/search` now calls `RetrievalService.search()`.
    - `mode_used` is "lexical" when the reason says "keyword search only",
  otherwise "hybrid". It reports what actually ran, whatever mode was requested.
    - `index_available` stays true when the only reason is keyword-only.
    - `degraded_reason` is passed through, cut to 300 characters.
    - Project detail `recent_memories` is unchanged and still works.
  - `proposals.py`: `_record_memory` now uses `memory_content_for(item,
  source)`.
  - `deps.py`: `/v1/health` adapters now include `embedder_engine` (the embedder
  class name), `embedder_model`, and `semantic_search` ("available" or
  "keyword_only").
  - `/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/wave50-c-api/services/
  api/tests/test_connectors_queue_changes.py` (new, 10 tests). They cover:
    - bad paths and the `.ssh` folder refused with 422
    - create writes exactly one full-sync event
    - pause, then a stale-revision 409, then sync on a paused connector refused
    - sync-now job id matches the outbox event, and delete returns 204 then 404
    - dead-letter list and retry are owner-scoped
    - change feed bootstrap, `reset`, timeouts with `wait=0` and `wait=1`
    - the long-poll wakes on a write and another request is served while it
  waits
    - search over a tmp folder after draining the worker returns a file citation
  (`uri` is the absolute path, `matched` includes "keyword", mode "lexical")
    - health reports the search mode

  **Deviations**
  - I added `GET /v1/connectors/{id}`, which is not in the table. It is a small
  read used by the delete test.
  - Sync-now returns 422 `invalid_state_transition` for a paused or needs_auth
  connector, rather than a 202 the worker would silently skip.
  - Patch refuses to change the status of a needs_auth connector.
  - A `kinds` filter on search keeps memory hits only, because corpus hits have
  no memory kind. It is applied after the retrieval limit.
  - `embedder_engine` reports the class of the embedder actually wired (e.g.
  `FixtureEmbedder`), not the `METIS_EMBED_ENGINE` env value.
  - The long-poll wakes only on entity changes, not on queue-count changes. A
  new dead letter therefore reaches a waiting client within one wait window (at
  most 25s).

  No contract problems.

⏺ API agent done (603 pass). Merging and running full checks, then a real
  end-to-end smoke test.

⏺ Bash(git diff --name-only main...wave50-c-api | grep -v '^services/api/'; git
      merge -q --no-edit -m "merge: wave50 API connectors, queue, change feed,
      hybrid search…)
  ⎿  6ac159e merge: wave50 API connectors, queue, change feed, hybrid searc
     h
     603 passed, 1 skipped, 2 deselected in 65.82s (0:01:05)
     … +6 lines (ctrl+o to expand)
  ⎿  Updated services/api/src/metis_api/deps.py +15
  ⎿  Updated services/api/src/metis_api/main.py +2 -2
  ⎿  Updated services/api/src/metis_api/proposals.py +2 -2
  ⎿  Updated services/api/src/metis_api/routes/__init__.py +2 -2
  ⎿  Created services/api/src/metis_api/routes/connectors.py +248
  ⎿  … +2 files changed
     services/api/src/metis_api/routes/projects.py
     services/api/tests/test_connectors_queue_changes.py
  ⎿  (a git step in this command can move the working tree, so these may be
     its changes, not edits) (ctrl+o to expand)
  ⎿  (timeout 10m)

⏺ Agent "Agent C: API endpoints" finished · 6m 53s

⏺ Everything green: 603 Python, 129 web, mypy/ruff clean, contracts in sync. Now
  a real smoke test — API + 3 worker processes + a folder with a good file and
  a corrupt PDF.

  Ran 1 shell command · 1 failed (ctrl+o to expand)

⏺ No Ollama installed here, so semantic is expected to report keyword-only.
  Running the smoke test.

  Ran 1 shell command (ctrl+o to expand)

⏺ Smoke tests all pass, including the corrupt-PDF dead-letter handling and the
  refusal to index .ssh. Next I'll check the worker processes and the DLQ retry,
  then clean up. · summary

  Ran 1 shell command (ctrl+o to expand)

⏺ DLQ retry works (re-ran, still corrupt, back in DLQ — correct). But after kill
  -9 of the parent, 3 child processes were still alive 3s later. Investigating
  that orphan issue.

  Ran 1 shell command (ctrl+o to expand)

⏺ Direct run: children exit within 2s of parent death. Re-running the exact
  earlier setup (via uv run) to rule out a real bug.

  Ran 1 shell command (ctrl+o to expand)

⏺ Clean both ways — children exit within 2s of a hard parent kill. The earlier
  lingering processes were a timing artifact of my first test. Removing the five
  merged worktrees (branches kept).

  Ran 1 shell command (ctrl+o to expand)

⏺ The first slice is built and merged into main: local-folder import, hybrid
  search with citations, the queue with its dead-letter queue, multiple Python
  workers, and background sync. Nothing is pushed.

  Tests:
  - Python: 603 passed (571 before), mypy and ruff clean.
  - Web: 129 passed, and the build succeeds.
  - Rust desktop: all checks and tests pass on macOS.

  Real run: I started the API and three worker processes against a temporary
  folder holding a markdown report, a notes file, a corrupt PDF and a
  .ssh/id_rsa:
  - Search by file name and content: "report" and "revenue" both found
    Q3-report.md, returned as a citation with title, path, character span and a
    Keyword badge. "id_rsa" returned nothing, so the secret file was never
    indexed.
  - Secret folder refused: adding .ssh itself as a source was rejected with a
    validation error.
  - Dead-letter queue: broken.pdf went straight to it with "not a readable PDF".
    Retry re-ran it, and it went back there because the file is still corrupt.
  - Change feed: it reported the connector and source changes the web sync
    worker reacts to.
  - Honest status: with no Ollama, health and search both say "keyword search
    only".
  - Workers: one interactive and two ingestion processes ran. After a hard kill
    of the parent, all of them exited within 2 seconds.

  What exists now:
  - Search: keyword and semantic search run side by side and their results are
    merged, over your memories and imported files. Each result carries a
    citation and the planner gets the same evidence. Built with LangChain's
    core, text-splitter and Ollama packages, with tracing forced off so file
    contents stay on your machine.
  - Queue: the existing outbox, split into an interactive lane and an ingestion
    lane with a cap on concurrent ingestion. Malformed input goes to the
    dead-letter queue, which can be listed and retried.
  - Sync: the workers re-sync each folder on a timer. In the app, a Web Worker
    follows the change feed and each screen reloads only what it shows; the
    15-second refresh loop on the main page is gone.
  - Sources screen: add a folder with the native folder picker, pause, resume,
    "Sync now", remove, per-lane queue counts, and dead letters with Retry.
  - Desktop (Rust): an Ollama check, the folder picker, "reveal in
    Finder/Explorer", settings passed to the workers, and build targets for
    macOS, Windows and Linux.

  Not tested yet:
  - Semantic search with Ollama: Ollama isn't installed here. Install it and run
    ollama pull nomic-embed-text to turn it on.
  - Windows and Linux builds: only the Mac build target is installed. Dictation
    into the focused app is stubbed outside macOS.
  - Packaged desktop backend: the worker processes haven't been run inside the
    slow PyInstaller build.
  - Desktop app UI: I tested the API directly, not through the app's screens.

  Not built yet:
  - Gmail and Drive. They need a Google OAuth client, and the spec's
    verification decision.
  - Email and Word document parsers.
  - Faster vector search (sqlite-vec).
  - Re-indexing everything after switching embedding model.

  Two things for you:
  - Google Drive sync: it hard-links the repo's git files into
    ~/Downloads/.tmp.driveupload. That's what blocked the agents' isolated
    workspaces, so you may want to exclude Metis from Drive.
  - Still open from the spec:
    - Q6: the AI extraction step needs a replacement for Bedrock, either a local
      Ollama model or the Anthropic API with your own key.
    - Q7: on Linux's Wayland display system, global hotkeys and simulated typing
      are restricted. Accept a degraded experience there, or start with X11
      only?

✻ Crunched for 2h 14m 9s · done Saturday 11:29 PM

※ recap: Making Metis a local-first desktop app with LangChain hybrid search, queued workers with a dead-letter queue, and background sync; the first slice is built, tested and merged on main. Next: install Ollama to try semantic search, then decide on Gmail and Drive. (turn off recaps in /config)
  ⎿  Read services/pipeline/src/metis_pipeline/dispatcher.py (339 lines)
  ⎿  Read ../../../../private/tmp/claude-501/-Users-nityanthmaramreddy-Downloads
     -Metis/a5da5ebf-fb17-4a90-ad14-7bcc718f69cf/scratchpad/briefs/C-api.md (34 
     lines)
  ⎿  Read ../../../../private/tmp/claude-501/-Users-nityanthmaramreddy-Downloads
     -Metis/a5da5ebf-fb17-4a90-ad14-7bcc718f69cf/scratchpad/briefs/E-desktop.md 
     (16 lines)
  ⎿  Read ../../../../private/tmp/claude-501/-Users-nityanthmaramreddy-Downloads
     -Metis/a5da5ebf-fb17-4a90-ad14-7bcc718f69cf/scratchpad/briefs/D-web.md (21 
     lines)
  ⎿  Read 
     ../../../../private/tmp/claude-501/-Users-nityanthmaramreddy-Downloads-Meti
     s/a5da5ebf-fb17-4a90-ad14-7bcc718f69cf/scratchpad/briefs/B-retrieval.md (25
     lines)
  ⎿  Skills restored (ponytail)

⏺ Compacted while idle, before the prompt cache expired · turn off with 
  "idleCompaction": false in settings.json

❯ /compact                                                                      
  ⎿  Compacted (ctrl+o to see full summary)
  ⎿  Read ../../../../private/tmp/claude-501/-Users-nityanthmaramreddy-Downloads
     -Metis/a5da5ebf-fb17-4a90-ad14-7bcc718f69cf/scratchpad/briefs/C-api.md (34 
     lines)
  ⎿  Read services/pipeline/src/metis_pipeline/dispatcher.py (339 lines)  ⎿  Read ../../../../private/tmp/claude-501/-Users-nityanthmaramreddy-Downloads
     -Metis/a5da5ebf-fb17-4a90-ad14-7bcc718f69cf/scratchpad/briefs/E-desktop.md 
     (16 lines)
  ⎿  Read ../../../../private/tmp/claude-501/-Users-nityanthmaramreddy-Downloads
     -Metis/a5da5ebf-fb17-4a90-ad14-7bcc718f69cf/scratchpad/briefs/D-web.md (21 
     lines)
  ⎿  Read 
     ../../../../private/tmp/claude-501/-Users-nityanthmaramreddy-Downloads-Meti
     s/a5da5ebf-fb17-4a90-ad14-7bcc718f69cf/scratchpad/briefs/B-retrieval.md (25
     lines)
  ⎿  Skills restored (ponytail)

❯ sumamrize what has been implemented so far for RAG. has anything regarding    
  query translation been introduced? i was considering using the langchain      
  library and specifically decomposition. like the langhchain library and       
  implement sequential decomposition alongside parallel decomposition in the    
  RAG implementation for our desktop app. also can you write integrated tests   
  that confirm whether                                                          
  our dead letter queues are picking up malformed PDFs and then retrying them.  
  Also, can we add an idempotency key for our dead letter queue?                
                                                                                
  If we wanted to retry a malformed PDF, a PDF that failed three times, we      
  realize that it failed, then we find that it's fine, and then we run it       
  through the queue again. There needs to be an idempotency key. Also, to       
  reduce dedups, we should introduce this idempotency key.                      
                                                                                
  Whenever a job fails on a worker, it goes back and gets put in the queue.     
  Instead of having that work be redone again, the worker will notice that      
  there's a hash that is some type of idempotency key. That would basically     
  prevent it from redoing the work all over again.                              

  Read 2 files, ran 4 shell commands · 1 failed (ctrl+o to expand)

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis; sed -n 108,120p
      services/retrieval/src/metis_retrieval/connectors.py
      python3 - <<'PY'…)
  ⎿              rel = path.relative_to(root).as_posix()
                 if path.is_symlink() or _excluded(path, rel, exclude_globs
     ):
     … +60 lines (ctrl+o to expand)
  ⎿  Updated apps/web/src/lib/api.test.ts +1 -1
      267      await api.syncConnector("c1", "k2");
      268      await api.queue();
      269      await api.deadLetters();
      270 -    await api.retryDeadLetter("e1");                          
      270 +    await api.retryDeadLetter("e1", "k3");                    
      271      await api.changes(41, 25);
      272      await api.search({ query: "report" });
      273      expect(calls.map((c) => `${c.init.method} ${c.url.replace(
           API_BASE, "")}`)).toEqual([
  ⎿  Updated apps/web/src/lib/api.ts +3 -2
      371      request<JobAccepted>(`/v1/connectors/${connectorId}/sync`,
            { method: "POST", idempotencyKey }),
      372    queue: () => request<QueueStats>("/v1/queue"),
      373    deadLetters: () => request<DeadLetterPage>("/v1/queue/dead-l
           etters"),
      374 -  retryDeadLetter: (eventId: string) =>                       
      375 -    request<Record<string, never>>(`/v1/queue/dead-letters/${e
          -ventId}/retry`, { method: "POST" }),                          
      374 +  /** Same key = same retry. Derive it from the failure so a d
          +ouble-click cannot requeue twice. */                          
      375 +  retryDeadLetter: (eventId: string, idempotencyKey: string) =
          +>                                                             
      376 +    request<{ event_id: string }>(`/v1/queue/dead-letters/${ev
          +entId}/retry`, { method: "POST", idempotencyKey }),           
      377    /** The sync worker polls this itself; exposed here for one-
           off reads. */
      378    changes: (after: number, wait = 0, signal?: AbortSignal) =>
      379      request<ChangesResponse>(`/v1/changes?after=${after}&wait=
           ${wait}`, { signal }),
  ⎿  Updated apps/web/src/screens/SourcesScreen.tsx +2 -1
      214    const retry = async () => {
      215      setBusy(true);
      216      try {
      217 -      await api.retryDeadLetter(d.event_id);                  
      217 +      // updated_at changes when a retry fails again, so the n
          +ext attempt gets a new key.                                   
      218 +      await api.retryDeadLetter(d.event_id, `dlq:${d.event_id}
          +:${d.updated_at}`.replace(/[^A-Za-z0-9_.:=-]/g, "-").slice(0, 
          +128));                                                        
      219        onRetried({ tone: "info", text: `Queued “${d.title ?? d.
           event_type}” again. It has not run yet.` });
      220      } catch (cause) {
      221        onRetried({ tone: "bad", text: mutationError("This item"
           , cause).text });
  ⎿  Updated services/api/src/metis_api/routes/connectors.py +7 -1
      220  
      221  @router.post("/queue/dead-letters/{event_id}/retry", status_co
           de=status.HTTP_202_ACCEPTED)
      222  def retry_dead_letter(event_id: str, ctx: Context) -> dict[str
           , Any]:
      223 +    """Requeue once per key: a double-click replays instead of
          + answering 404."""                                            
      224 +    ctx.require_idempotency_key()                             
      225 +    if replayed := ctx.replay():                              
      226 +        return replayed                                       
      227      if not ctx.state.requeue_outbox(ctx.user_id, event_id):
      228          raise MetisError(ErrorCode.NOT_FOUND, f"dead letter {e
           vent_id} not found")
      225 -    return {}                                                 
      229 +    response = {"event_id": event_id}                         
      230 +    ctx.remember(response)                                    
      231 +    return response                                           
      232  
      233  
      234  @router.get("/changes", response_model=ChangesResponse)
  ⎿  Updated services/api/tests/test_connectors_queue_changes.py +8 -3
      115      ]
      116      ingest = next(lane for lane in client.get("/v1/queue").jso
           n()["lanes"] if lane["lane"] == "ingest")
      117      assert ingest["dead_letter"] == 1
      118 -    assert client.post("/v1/queue/dead-letters/obx_theirs/retr
          -y").status_code == 404                                        
      119 -    assert client.post("/v1/queue/dead-letters/obx_mine/retry"
          -).status_code == 202                                          
      120 -    assert client.post("/v1/queue/dead-letters/obx_mine/retry"
          -).status_code == 404, "no longer dead"                        
      118 +    url = "/v1/queue/dead-letters/{}/retry"                   
      119 +    assert client.post(url.format("obx_mine")).status_code == 
          +422, "retry requires an Idempotency-Key"                      
      120 +    assert client.post(url.format("obx_theirs"), headers=key()
          +).status_code == 404                                          
      121 +    same = key()                                              
      122 +    assert client.post(url.format("obx_mine"), headers=same).s
          +tatus_code == 202                                             
      123 +    replay = client.post(url.format("obx_mine"), headers=same)
      124 +    assert replay.status_code == 202 and replay.json() == {"ev
          +ent_id": "obx_mine"}, "double-click replays"                  
      125 +    assert client.post(url.format("obx_mine"), headers=key()).
          +status_code == 404, "no longer dead"                          
      126      assert client.get("/v1/queue/dead-letters").json()["items"
           ] == []
      127  
      128  
  ⎿  … +1 file changed
     services/retrieval/src/metis_retrieval/corpus_store.py

⏺ Receipts and the retry key are in. Next I'm wiring the hash check into the
  ingest worker, then writing the integration tests.

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis
      python3 - <<'PY'…)
  ⎿      state_store = SqliteStateStore(str(tmp_path / "state.sqlite"), clo
     ck=clock)
         transport = FixtureTransport()
     … +71 lines (ctrl+o to expand)
  ⎿  Created services/api/tests/test_dead_letter_integration.py +199
        1 +"""Real path: API -> outbox -> dispatcher -> parser -> dead le
          +tter -> retry."""                                             
        2 +                                                              
        3 +from __future__ import annotations                            
        4 +                                                              
        5 +import uuid                                                   
        6 +from pathlib import Path                                      
        7 +                                                              
        8 +import pytest                                                 
        9 +from metis_contracts.enums import SourceLifecycle             
       10 +from metis_pipeline.dispatcher import DispatchContext, OutboxD
          +ispatcher                                                     
       11 +from metis_pipeline.local_worker import build_registry        
       12 +from metis_retrieval import handlers                          
       13 +                                                              
       14 +USER = "demo_authenticated_subject"                           
       15 +MALFORMED = b"%PDF-1.4 truncated"                             
       16 +                                                              
       17 +                                                              
       18 +def valid_pdf(text: str) -> bytes:                            
       19 +    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
       20 +    objects = [                                               
       21 +        b"<< /Type /Catalog /Pages 2 0 R >>",                 
       22 +        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",         
       23 +        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]
          + /Contents 4 0 R "                                            
       24 +        b"/Resources << /Font << /F1 5 0 R >> >> >>",         
       25 +        b"<< /Length %d >>\nstream\n" % len(stream) + stream +
          + b"\nendstream",                                              
       26 +        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica 
          +>>",                                                          
       27 +    ]                                                         
       28 +    out, offsets = bytearray(b"%PDF-1.4\n"), []               
       29 +    for number, body in enumerate(objects, start=1):          
       30 +        offsets.append(len(out))                              
       31 +        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"  
       32 +    xref = len(out)                                           
       33 +    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects
          +) + 1)                                                        
       34 +    out += b"".join(b"%010d 00000 n \n" % offset for offset in
          + offsets)                                                     
       35 +    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%
          +d\n%%%%EOF\n" % (len(objects) + 1, xref)                      
       36 +    return bytes(out)                                         
       37 +                                                              
       38 +                                                              
       39 +def key() -> dict[str, str]:                                  
       40 +    return {"Idempotency-Key": uuid.uuid4().hex}              
     … +159 lines (ctrl+o to expand)
  ⎿  Updated services/retrieval/src/metis_retrieval/corpus_store.py +3 -1
      229          for chunk_id in ids:
      230              self._conn.execute("DELETE FROM chunks_fts WHERE u
           ser_id=? AND chunk_id=?", (user_id, chunk_id))
      231          self._conn.execute("DELETE FROM chunks WHERE user_id=?
            AND source_id=?", (user_id, source_id))
      232 -        self._conn.execute("DELETE FROM ingest_receipts WHERE 
          -user_id=? AND source_id=?", (user_id, source_id))             
      232 +        self._conn.execute(                                   
      233 +            "DELETE FROM ingest_receipts WHERE user_id=? AND s
          +ource_id=?", (user_id, source_id)                             
      234 +        )                                                     
      235          return len(ids)
      236  
      237      # -- reads -----------------------------------------------
           ---------- #
  ⎿  Updated services/retrieval/src/metis_retrieval/handlers.py +24 -1
      2  
      3  from __future__ import annotations
      4  
      5 +import hashlib                                                  
      6  import logging
      7  from pathlib import Path
      8  from typing import Any
     ...
      12  from metis_contracts.events import ConnectorSyncPayload, Memory
          IndexPayload, PurgePayload, SourceIngestPayload
      13  from metis_pipeline.dispatcher import DispatchContext, HandlerR
          egistry, classify
      14  
      14 -from .connectors import sync_connector                         
      15 +from .connectors import _sha256, sync_connector                
      16  from .corpus_store import ChunkRow, chunk_id_for
      17  from .embedder import content_hash
      18  from .ingestion import chunk_text, needs_embedding
     ...
      143              error_message=f"skipped: {size} bytes exceeds the 
           {limit}-byte corpus limit",
      144          )
      145          return
      146 +                                                              
      147 +    # Same bytes + version + model = same result, so a redeliv
          +ered job skips the                                            
      148 +    # parse and embed. Only a finished ingest writes a receipt
          +; failures rerun.                                             
      149 +    model_id = context.embedder.model_id if context.embedder i
          +s not None else ""                                            
      150 +    work_key = hashlib.sha256(                                
      151 +        f"{source.id}:{source.current_version}:{_sha256(path)}
          +:{model_id}".encode()                                         
      152 +    ).hexdigest()                                             
      153 +    done = corpus.ingested_chunk_count(user_id, source.id, wor
          +k_key)                                                        
      154 +    if done is not None:                                      
      155 +        log.info("source %s already ingested for this content;
          + skipping", source.id)                                        
      156 +        if source.lifecycle is not SourceLifecycle.READY:  # c
          +rashed after indexing                                         
      157 +            _mark(                                            
      158 +                context,                                      
      159 +                source,                                       
      160 +                lifecycle=SourceLifecycle.READY,              
      161 +                chunk_count=done,                             
      162 +                failure=None,                                 
      163 +                error_message=None,                           
      164 +            )                                                 
      165 +        return                                                
      166 +                                                              
      167      try:
     … +9 lines (ctrl+o to expand)
  ⎿  (timeout 5m)

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis;
      f=services/api/tests/test_dead_letter_integration.py
      sed -i '' 's/tmp_path/docs/g' $f…)
  ⎿  ......
        [100%]
     6 passed in 1.05s
     … +16 lines (ctrl+o to expand)
  ⎿  Updated services/api/tests/test_dead_letter_integration.py +26 -20
      40      return {"Idempotency-Key": uuid.uuid4().hex}
      41  
      42  
      43 +@pytest.fixture()                                              
      44 +def docs(tmp_path) -> Path:                                    
      45 +    """Separate from tmp_path, which also holds the test databa
         +ses."""                                                        
      46 +    folder = tmp_path / "docs"                                 
      47 +    folder.mkdir()                                             
      48 +    return folder                                              
      49 +                                                               
      50 +                                                               
      51  @pytest.fixture()
      52  def drain(adapters, clock):
      53      context = DispatchContext(
     ...
       99      return client.post(f"/v1/queue/dead-letters/{event_id}/ret
           ry", headers=headers or key())
      100  
      101  
       94 -def test_malformed_pdf_dead_letters_once_without_auto_retry(cl
          -ient, adapters, drain, calls, tmp_path):                      
       95 -    (tmp_path / "broken.pdf").write_bytes(MALFORMED)          
       96 -    connect(client, tmp_path)                                 
      102 +def test_malformed_pdf_dead_letters_once_without_auto_retry(cl
          +ient, adapters, drain, calls, docs):                          
      103 +    (docs / "broken.pdf").write_bytes(MALFORMED)              
      104 +    connect(client, docs)                                     
      105      drain()
      106  
      107      [letter] = dead(client)
     ...
      113      assert lanes["ingest"]["dead_letter"] == 1
      114  
      115  
      108 -def test_retry_of_still_malformed_pdf_returns_to_dead_letters(
          -client, drain, calls, tmp_path):                              
      109 -    (tmp_path / "broken.pdf").write_bytes(MALFORMED)          
      110 -    connect(client, tmp_path)                                 
      116 +def test_retry_of_still_malformed_pdf_returns_to_dead_letters(
          +client, drain, calls, docs):                                  
      117 +    (docs / "broken.pdf").write_bytes(MALFORMED)              
      118 +    connect(client, docs)                                     
      119      drain()
      120      [letter] = dead(client)
      121  
     ...
      128      assert calls["parse"] == 2, "an explicit retry really reru
           ns the parser"
      129  
     … +56 lines (ctrl+o to expand)
  ⎿  (timeout 10m)

  Ran 1 shell command (ctrl+o to expand)

⏺ Dead-letter tests and idempotency keys are done, but query translation doesn't
  exist yet. The work is committed to branch dlq-idempotency (4968d52), not
  merged. Full Python suite: 609 passed, 1 skipped. All 129 web tests pass, and
  ruff, mypy and typecheck are clean.

  RAG today

  - Ingestion: local folders only, covering text, Markdown, code, CSV and PDF.
    Gmail and Drive are not built.
  - Chunking: LangChain RecursiveCharacterTextSplitter, about 2000 characters
    with 240 overlap.
  - Storage: chunks live in memory.sqlite, with FTS5 over title, path and
    content, plus float32 vectors.
  - Embeddings: Ollama nomic-embed-text. Without it, search falls back to
    keyword-only and says so. Real Ollama semantic search is still untested.
  - Retrieval: semantic + keyword search over memories and files, fused with
    weighted RRF. Files are capped at 50% of results.
  - Safety: every hit is rechecked for owner, tombstone and current version.
  - Citations: origin, title, path, character span, excerpt, and
    semantic/keyword match flags.
  - Query translation: none. The raw query goes straight to the retrievers, with
    no decomposition, multi-query or HyDE.

  Dead-letter queue

  ┌────────────────────────┬────────────────────────────────────────────────┐
  │        Scenario        │                 Tested result                  │
  ├────────────────────────┼────────────────────────────────────────────────┤
  │ Malformed PDF          │ Dead-letters on attempt 1, no automatic retry  │
  ├────────────────────────┼────────────────────────────────────────────────┤
  │ Retry, still broken    │ Parser reruns, returns to DLQ                  │
  ├────────────────────────┼────────────────────────────────────────────────┤
  │ Repaired, then retried │ Indexed and searchable                         │
  ├────────────────────────┼────────────────────────────────────────────────┤
  │ Retry double-click     │ Requeued and parsed once                       │
  ├────────────────────────┼────────────────────────────────────────────────┤
  │ Worker dies after      │ Redelivered job skips parse and embed, marks   │
  │ indexing               │ source ready                                   │
  ├────────────────────────┼────────────────────────────────────────────────┤
  │ File bytes change      │ Never skipped, always reprocessed              │
  └────────────────────────┴────────────────────────────────────────────────┘

  Malformed files don't fail three times, because retrying can't fix them. Only
  transient failures like Ollama being down retry, up to 5 attempts.

  There are two keys:
  - Retry key: POST /dead-letters/{id}/retry now requires an Idempotency-Key.
  - Work hash: sha256(source + version + file bytes + embedding model), saved
    only after an ingest fully finishes. Failed work has no receipt, so it can
    still be retried.

  Problems I found (not fixed)

  1. Unsupported binaries flood the DLQ. Any image or database file in a watched
     folder becomes a dead letter. These should be skipped with a reason
     instead.
  2. Stale dead letters linger. From reading the code, if a file is fixed and
     the next sync indexes it, the old entry stays in the DLQ until someone
     retries it.

  Decomposition plan

  LangChain's core packages have no ready-made decomposer, so I'd build it with
  langchain-core runnables and keep our hybrid retriever:
  - Parallel: independent sub-questions run through RunnableParallel, then merge
    with RRF. Example: "compare Q3 and Q4 revenue."
  - Sequential: each step's answer feeds the next query. Example: "who owns the
    project in my last email about the launch?"
  - Router: simple queries skip decomposition, so normal search adds no LLM
    latency. If the LLM fails, search uses the original query.

  Should decomposition run on a local Ollama chat model? I recommend yes: file
  and email text stays on the device, and langchain-ollama is already installed.
  The tradeoff is slower, weaker planning than a hosted model. If you agree,
  I'll build it and fix the two DLQ problems first.

✻ Cogitated for 3m 58s · done 2:04 PM

❯ yes i agree with the logic here. start writing the code here. and then i want 
  you to see the status of the tauri desktop. for some reason there is a lot    
  of latencyWe need to make sure that the semantic search for Ollama is tested. 
  I think that might require a run, but if that's the case, then we can do      
  that later. That should be a note made in documentation for next steps.       
  Yeah, we should skip those unsupported binaries, like images or database      
  files, until we actually add support for the ability to read that and have    
  the workers be able to process that information.                              
  If there are any dead letters that are lingering, there cannot always be      
  letters that linger in the dead letter queue. It should be like this whole    
  first-in, first-out implementation, because we need to dequeue anything at    
  the end of the dead letter queue. Otherwise, we can run into issues there.    
  Yes, the decomposition plan looks good for the most part. I like that. Yes, I 
  like this. Run decomposition first on a local Ollama chat model so that I     
  can see if it works, and then we can scale it to something on the cloud that  
  we can put into production, because that's important.                         
  Note that there are two other agents:                                         
  - Another Claude Code agent is running, fixing up some issues with the queue  
  itself.                                                                       
  - A third Codex agent is working on some system design issues that we've      
  noticed.                                                                      

⏺ Two other agents are active, so I'm checking what they've touched before I
  edit anything.

  Ran 1 shell command (ctrl+o to expand)

  Ran 2 shell commands · 1 failed (ctrl+o to expand)
  ⎿  “Announce rag-decomposition worktree and queue files” → sent to 
     metis-d1 — another Claude session on this machine; it is also 
     connected via Remote Control

  Ran 2 shell commands (ctrl+o to expand)

❯ Since it’s the built app, I’d narrow it down by one key detail: does the      
  blank window eventually show the UI, or stay blank forever?                   
                                                                                
  If it eventually appears, the strongest suspect is frontend startup code      
  waiting for something before it renders: a Rust invoke, database              
  initialization, authentication check, or network request. The window exists,  
  but your app hasn’t drawn its first screen yet.                               
                                                                                
  If it stays blank, I’d check these first:                                     
  Possible cause    What it means                                               
  Missing or mislocated frontend files    frontendDist points to the wrong      
  build folder, or index.html cannot load its JavaScript files. Tauri’s built   
  app needs those frontend assets. v2.tauri.app                                 
  JavaScript crashes on launch    The page loads, but an error stops your UI    
  from rendering.                                                               
  Development URL left in the app    Frontend code tries to reach localhost or  
  another service that only exists when you run tauri dev.                      
  Frontend requires a server    If you’re using a framework mode that expects   
  server rendering, the packaged Tauri app won’t provide that server            
  automatically. Tauri’s frontend guidance calls for static or client rendered  
  output. v2.tauri.app                                                          
  Built app configuration differs    A production environment variable, API     
  URL, or asset path differs from development and breaks startup.               
                                                                                
  The fastest way to tell: make a debug build of the packaged app and open its  
  web inspector. Tauri enables the inspector in debug builds; its Console and   
  Network tabs will show whether a file failed to load, JavaScript threw an     
  error, or a request is hanging. tauri.app                                     

⏺ Checking the blank-window path: what the UI waits on before its first paint,
  since each sidecar takes 6.5–9.8s to cold-start.

  Ran 2 shell commands (ctrl+o to expand)

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis/.worktrees/rag-decomposition
      sed -n 393,408p packages/contracts/src/metis_contracts/api.py; sed -n
      120,135p package…)
  ⎿  class SearchResult(StrictModel):
         """One hybrid-retrieval hit. ``evidence`` is the citation shown to
      the user.
     … +81 lines (ctrl+o to expand)
  ⎿  Updated .worktrees/rag-decomposition/packages/contracts/src/metis_cont
     racts/api.py +2
      412      index_available: bool = True
      413      index_stale: bool = False
      414      degraded_reason: Annotated[str | None, Field(default=None,
            max_length=300)] = None
      415 +    decomposition: Literal["none", "parallel", "sequential"] =
          + "none"                                                       
      416 +    sub_queries: list[Annotated[str, Field(max_length=1000)]] 
          += Field(default_factory=list, max_length=5)                   
      417  
      418  
      419  class ProjectDetail(StrictModel):
  ⎿  Updated .worktrees/rag-decomposition/packages/contracts/src/metis_cont
     racts/config.py +6
      122          default_factory=lambda: _env_int("METIS_CORPUS_MAX_FIL
           E_BYTES", 20 * 1024 * 1024)
      123      )
      124      #: Share of retrieval candidates the corpus lane may take 
           (percent).
      125 +    dead_letter_max_per_user: int = field(                    
      126 +        default_factory=lambda: _env_int("METIS_DEAD_LETTER_MA
          +X_PER_USER", 200)                                             
      127 +    )                                                         
      128 +    dead_letter_retention_days: int = field(                  
      129 +        default_factory=lambda: _env_int("METIS_DEAD_LETTER_RE
          +TENTION_DAYS", 30)                                            
      130 +    )                                                         
      131      retrieval_corpus_share_pct: int = field(
      132          default_factory=lambda: _env_int("METIS_RETRIEVAL_CORP
           US_SHARE", 50)
      133      )
  ⎿  Updated .worktrees/rag-decomposition/packages/contracts/src/metis_cont
     racts/enums.py +2
      292      FAILED = "failed"
      293      DEAD_LETTER = "dead_letter"
      294      SUPERSEDED = "superseded"
      295 +    #: Aged or pushed out of the dead-letter queue, oldest fir
          +st. Kept for audit.                                           
      296 +    EXPIRED = "expired"                                       
      297  
      298  
      299  class AdapterMode(StrEnum):
  ⎿  Updated .worktrees/rag-decomposition/services/pipeline/src/metis_pipel
     ine/local_worker.py +16 -1
      168      while True:
      169          result = dispatcher.run_once()
      170          due = (
      171 -            _fire_due_reminders(context) + _fire_due_daily_pla
          -ns(context) + _fire_due_syncs(context)                        
      171 +            _fire_due_reminders(context)                      
      172 +            + _fire_due_daily_plans(context)                  
      173 +            + _fire_due_syncs(context)                        
      174 +            + _expire_dead_letters(context)                   
      175              if timers
      176              else 0
      177          )
     ...
      359      return sum(r["enqueued"] for r in results)
      360  
      361  
      362 +def _expire_dead_letters(context: Any) -> int:                
      363 +    """Age out dead letters even when nothing new is failing."
          +""                                                            
      364 +    evict = getattr(context.state_store, "evict_dead_letters",
          + None)                                                        
      365 +    if evict is None:                                         
      366 +        return 0                                              
      367 +    try:                                                      
      368 +        return int(evict())                                   
      369 +    except Exception:                                         
      370 +        log.exception("dead-letter expiry failed; will retry n
          +ext cycle")                                                   
      371 +        return 0                                              
      372 +                                                              
      373 +                                                              
      374  def _fire_due_syncs(context: Any) -> int:
      375      """Re-sync connectors whose interval has passed (D10). met
           is_retrieval owns
      376      the logic; absent or older retrieval means nothing to do."
           ""
  ⎿  Updated .worktrees/rag-decomposition/services/retrieval/src/metis_retr
     ieval/connectors.py +4 -1
      37  )
      38  from metis_contracts.enums import FailureCategory, SourceKind, 
          SourceLifecycle
      39  
      40 +from .parsers import is_indexable                              
      41 +                                                               
      42  log = logging.getLogger("metis.retrieval.connectors")
      43  
      44  #: Matched (case-insensitively) against every path component. N
          ot overridable.
     ...
      110              rel = path.relative_to(root).as_posix()
      111              if path.is_symlink() or _excluded(path, rel, exclu
           de_globs):
      112                  continue
      111 -            if _included(path, rel, include_globs):           
      113 +            # ponytail: reads 8 KiB per file each sync; cache 
          +by mtime if huge folders drag.                                
      114 +            if _included(path, rel, include_globs) and is_inde
          +xable(path):                                                  
      115                  yield path
      116  
      117  
  ⎿  … +3 files changed
     .worktrees/rag-decomposition/services/retrieval/src/metis_retrieval/ha
     ndlers.py
     .worktrees/rag-decomposition/services/retrieval/src/metis_retrieval/pa
     rsers.py
     .worktrees/rag-decomposition/services/state/src/metis_state/sqlite_sto
     re.py

@ metis-d1❯
  No overlap on queue functions: this session has not touched complete_outbox,
  requeue_outbox, list_dead_letters or claim_outbox.
  I work in ../metis-port-agents on branch port-agent-work, cut from main at
  535bad5, and I am not the session committing to dlq-idempotency. My only
  sqlite_store.py change is +8 lines in _Tx (around line 125):
  remember_idempotent now raises IDEMPOTENCY_MISMATCH if the key already exists.
  Expect a small merge there only if you edit that method.

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis/.worktrees/rag-decomposition
      sed -i '' '0,/^    settings,$/s//    EventType,\n    settings,/'
      services/state/src/me…)
  ⎿  0
     wrote 115 artifacts to schemas and generated
     synced apps/web/src/contracts/metis.ts
     … +54 lines (ctrl+o to expand)
  ⎿  Updated .worktrees/rag-decomposition/apps/web/src/contracts/metis.ts
     +3
      131    FAILED = "failed",
      132    DEAD_LETTER = "dead_letter",
      133    SUPERSEDED = "superseded",
      134 +  EXPIRED = "expired",                                        
      135  }
      136  
      137  export enum PlanStatus {
     ...
      1218    index_available?: boolean;
      1219    index_stale?: boolean;
      1220    degraded_reason?: string | null;
      1221 +  decomposition?: "none" | "parallel" | "sequential";        
      1222 +  sub_queries?: string[];                                    
      1223  }
      1224  
      1225  /** One hybrid-retrieval hit. ``evidence`` is the citation sh
            own to the user. */
  ⎿  Updated
     .worktrees/rag-decomposition/packages/contracts/generated/metis.ts +3
      131    FAILED = "failed",
      132    DEAD_LETTER = "dead_letter",
      133    SUPERSEDED = "superseded",
      134 +  EXPIRED = "expired",                                        
      135  }
      136  
      137  export enum PlanStatus {
     ...
      1218    index_available?: boolean;
      1219    index_stale?: boolean;
      1220    degraded_reason?: string | null;
      1221 +  decomposition?: "none" | "parallel" | "sequential";        
      1222 +  sub_queries?: string[];                                    
      1223  }
      1224  
      1225  /** One hybrid-retrieval hit. ``evidence`` is the citation sh
            own to the user. */
  ⎿  Updated .worktrees/rag-decomposition/packages/contracts/schemas/Outbox
     Event.json +2 -1
      25          "done",
      26          "failed",
      27          "dead_letter",
      28 -        "superseded"                                           
      28 +        "superseded",                                          
      29 +        "expired"                                              
      30        ],
      31        "title": "OutboxState",
      32        "type": "string"
  ⎿  Updated .worktrees/rag-decomposition/packages/contracts/schemas/Search
     Response.json +19
      258    "$schema": "https://json-schema.org/draft/2020-12/schema",
      259    "additionalProperties": false,
      260    "properties": {
      261 +    "decomposition": {                                        
      262 +      "default": "none",                                      
      263 +      "enum": [                                               
      264 +        "none",                                               
      265 +        "parallel",                                           
      266 +        "sequential"                                          
      267 +      ],                                                      
      268 +      "title": "Decomposition",                               
      269 +      "type": "string"                                        
      270 +    },                                                        
      271      "degraded_reason": {
      272        "anyOf": [
      273          {
     ...
      301        },
      302        "title": "Results",
      303        "type": "array"
      304 +    },                                                        
      305 +    "sub_queries": {                                          
      306 +      "items": {                                              
      307 +        "maxLength": 1000,                                    
      308 +        "type": "string"                                      
      309 +      },                                                      
      310 +      "maxItems": 5,                                          
      311 +      "title": "Sub Queries",                                 
      312 +      "type": "array"                                         
      313      }
      314    },
      315    "required": [
  ⎿  Updated
     .worktrees/rag-decomposition/packages/contracts/schemas/bundle.json
     +21 -1
      8938              "done",
      8939              "failed",
      8940              "dead_letter",
      8941 -            "superseded"                                     
      8941 +            "superseded",                                    
      8942 +            "expired"                                        
      8943            ],
      8944            "title": "OutboxState",
      8945            "type": "string"
     ...
      15608        "$id": "https://metis.local/schemas/v1/SearchResponse.
             json",
      15609        "additionalProperties": false,
      15610        "properties": {
      15611 +        "decomposition": {                                  
      15612 +          "default": "none",                                
      15613 +          "enum": [                                         
      15614 +            "none",                                         
      15615 +            "parallel",                                     
      15616 +            "sequential"                                    
      15617 +          ],                                                
      15618 +          "title": "Decomposition",                         
      15619 +          "type": "string"                                  
      15620 +        },                                                  
      15621          "degraded_reason": {
      15622            "anyOf": [
      15623              {
     ...
      15651            },
      15652            "title": "Results",
      15653            "type": "array"
      15654 +        },                                                  
      15655 +        "sub_queries": {                                    
      15656 +          "items": {                                        
      15657 +            "maxLength": 1000,                              
      15658 +            "type": "string"                                
      15659 +          },                                                
      15660 +          "maxItems": 5,                                    
      15661 +          "title": "Sub Queries",                           
      15662 +          "type": "array"                                   
      15663          }
      15664        },
      15665        "required": [
  ⎿  … +5 files changed
     .worktrees/rag-decomposition/services/api/src/metis_api/routes/project
     s.py
     .worktrees/rag-decomposition/services/api/tests/test_dead_letter_integ
     ration.py
     .worktrees/rag-decomposition/services/retrieval/src/metis_retrieval/de
     composition.py
     +2 more
  ⎿  (timeout 6m 40s)

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis/.worktrees/rag-decomposition
      python3 - <<'PY'…)
  ⎿  Found 1 error (1 fixed, 0 remaining).
     1 file reformatted, 120 files left unchanged
     FAILED tests/e2e/test_e2e_reminders.py::test_fixture_delivery_evidence
     … +20 lines (ctrl+o to expand)
  ⎿  Updated .worktrees/rag-decomposition/services/retrieval/src/metis_retr
     ieval/decomposition.py +10 -6
      11  
      12  from __future__ import annotations
      13  
      14 -import json                                                    
      14  import os
      15  import re
      16  from collections.abc import Callable
     ...
      22  from langchain_core.prompts import ChatPromptTemplate
      23  from langchain_core.runnables import RunnableLambda, RunnablePa
          rallel
      24  from metis_contracts.api import SearchResult
      26 -from pydantic import BaseModel, Field, ValidationError         
      25 +from pydantic import BaseModel, Field                          
      26  
      27  from .retrievers import RRF_C
      28  
     ...
      135                  return DecomposedSearch(*search(query))
      136              run = self._parallel if plan.mode == "parallel" el
           se self._sequential
      137              return run(query, plan.steps, search, limit)
      139 -        except (OSError, ValueError, ValidationError, json.JSO
          -NDecodeError, RuntimeError) as exc:                           
      138 +        except Exception as exc:  # model down, timeout, bad J
          +SON: search must still answer                                 
      139              found = DecomposedSearch(*search(query))
      140              found.note = f"query decomposition unavailable: {t
           ype(exc).__name__}"
      141              return found
      142  
      143      def _parallel(self, query: str, steps: list[str], search: 
           Search, limit: int) -> DecomposedSearch:
      144          queries = [query, *steps]  # the original query protec
           ts recall from a bad split
      146 -        lanes = RunnableParallel(                             
      147 -            {str(i): RunnableLambda(lambda _, q=q: search(q)) 
          -for i, q in enumerate(queries)}                               
      148 -        ).invoke(None)                                        
      145 +                                                              
      146 +        def lane(text: str) -> RunnableLambda[None, tuple[list
          +[SearchResult], str | None]]:                                 
      147 +            def run(_: None) -> tuple[list[SearchResult], str 
          +| None]:                                                      
      148 +                return search(text)                           
      149 +                                                              
      150 +            return RunnableLambda(run)                        
      151 +                                                              
      152 +        lanes = RunnableParallel({str(i): lane(q) for i, q in 
          +enumerate(queries)}).invoke(None)                             
      153          outcomes = [lanes[str(i)] for i in range(len(queries))
           ]
      154          return DecomposedSearch(
      155              results=fuse([results for results, _ in outcomes],
            limit=limit),
  ⎿  Updated .worktrees/rag-decomposition/services/state/src/metis_state/sq
     lite_store.py +1
      50  from metis_contracts.api import Change, LaneStats, QueueStats
      51  from metis_contracts.common import Record
      52  from metis_contracts.entities import ENTITY_MODELS, Availabilit
          yBlock, Job, Notification, Reminder
      53 +from metis_contracts.events import EventType                   
      54  
      55  SCHEMA = (Path(__file__).parent / "schema.sql").read_text()
      56  
  ⎿  (timeout 8m 20s)

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis/.worktrees/rag-decomposition
      sed -i '' 's/EventType.CORPUS_PURGE_REQUESTED.value/EventType.CORPUS_PURGE
      _REQUESTED/'…)
  ⎿  ......................................................................
     .. [ 11%]
     ......................................................................
     … +19 lines (ctrl+o to expand)
  ⎿  Updated .worktrees/rag-decomposition/services/state/src/metis_state/sq
     lite_store.py +1 -1
      710                         AND done.created_at>=outbox.created_at
      711                         AND (done.event_type=outbox.event_type 
           OR done.event_type=?)
      712                   )""",
      713 -            (now, user_id, event_id, event_id, EventType.CORPU
          -S_PURGE_REQUESTED.value),                                     
      713 +            (now, user_id, event_id, event_id, EventType.CORPU
          +S_PURGE_REQUESTED),                                           
      714          )
      715  
      716      def evict_dead_letters(self) -> int:
  ⎿  (timeout 5m)

⏺ All checks are green. I'm adding the Ollama live test and the next-steps note,
  then committing.
⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis/.worktrees/rag-decomposition
      grep -n 'def build_embedder' -A22
      services/retrieval/src/metis_retrieval/embedder.py |…)
  ⎿  234:def build_embedder() -> Any:
     235-    engine = embed_engine()
     236-    if engine == "cloud":
     … +16 lines (ctrl+o to expand)
  ⎿  Created .worktrees/rag-decomposition/ops/ollama-rag-verification.md
     +42
       1 +# Next step: verify RAG on real Ollama                         
       2 +                                                               
       3 +**Status: not verified.** Ollama is not installed on the develo
         +pment machine, so                                              
       4 +semantic search and query decomposition have only run against f
         +ixtures and fake                                               
       5 +chat models. Do not call either production-ready until this pas
         +ses.                                                           
       6 +                                                               
       7 +## Run                                                         
       8 +                                                               
       9 +```bash                                                        
      10 +brew install ollama                                            
      11 +ollama serve                      # separate terminal          
      12 +ollama pull nomic-embed-text                                   
      13 +ollama pull <chat-model>          # choose one that fits availa
         +ble memory                                                     
      14 +                                                               
      15 +METIS_LOCAL_LLM_MODEL=<chat-model> \                           
      16 +  uv run --frozen pytest -q -W ignore -o addopts="" -m integrat
         +ion \                                                          
      17 +  services/retrieval/tests/test_ollama_live.py                 
      18 +```                                                            
      19 +                                                               
      20 +The tests skip when Ollama or a model is missing. A skip is not
         + a pass.                                                       
      21 +                                                               
      22 +## Pass criteria                                               
      23 +                                                               
      24 +- A paraphrase with no shared keyword finds the right file, fla
         +gged `semantic`.                                               
      25 +- A comparison query plans `parallel` and cites both source fil
         +es.                                                            
      26 +- Record chat-model name, RAM use and p50/p95 search latency wi
         +th decomposition on.                                           
      27 +                                                               
      28 +## Enable in the app                                           
      29 +                                                               
      30 +```bash                                                        
      31 +METIS_EMBED_ENGINE=local                                       
      32 +METIS_LOCAL_EMBED_MODEL=nomic-embed-text                       
      33 +METIS_LOCAL_LLM_MODEL=<chat-model>                             
      34 +METIS_QUERY_DECOMPOSITION=local   # default: off               
      35 +```                                                            
      36 +                                                               
      37 +## Before moving decomposition to the cloud                    
      38 +                                                               
      39 +- Reserve and settle budget around every planner call (CLAUDE.m
         +d rule 7).                                                     
      40 +- Decide what file and email text may leave the device. Sequent
         +ial mode sends                                                 
     … +2 lines (ctrl+o to expand)
  ⎿  Created .worktrees/rag-decomposition/services/retrieval/tests/test_oll
     ama_live.py +94
       1 +"""Live Ollama checks. Run by hand: see ops/ollama-rag-verifica
         +tion.md."""                                                    
       2 +                                                               
       3 +from __future__ import annotations                             
       4 +                                                               
       5 +import json                                                    
       6 +import os                                                      
       7 +import urllib.request                                          
       8 +from datetime import UTC, datetime                             
       9 +                                                               
      10 +import pytest                                                  
      11 +from metis_contracts import Connector, ConnectorConfig, Connect
         +orKind, FixedClock, reset_settings_cache                       
      12 +from metis_pipeline.dispatcher import DispatchContext, OutboxDi
         +spatcher                                                       
      13 +from metis_retrieval import RetrievalService, SqliteMemoryStore
         +, build_embedder, semantic_available                           
      14 +from metis_retrieval.connectors import enqueue_sync            
      15 +from metis_retrieval.handlers import registry                  
      16 +from metis_state.sqlite_store import SqliteStateStore          
      17 +                                                               
      18 +pytestmark = pytest.mark.integration                           
      19 +                                                               
      20 +ENDPOINT = os.environ.get("METIS_LOCAL_LLM_ENDPOINT", "http://1
         +27.0.0.1:11434")                                               
      21 +EMBED_MODEL = os.environ.get("METIS_LOCAL_EMBED_MODEL", "nomic-
         +embed-text")                                                   
      22 +CHAT_MODEL = os.environ.get("METIS_LOCAL_LLM_MODEL", "")       
      23 +USER = "user_live"                                             
      24 +T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)                 
      25 +                                                               
      26 +                                                               
      27 +def installed() -> set[str]:                                   
      28 +    try:                                                       
      29 +        with urllib.request.urlopen(f"{ENDPOINT}/api/tags", tim
         +eout=2) as response:                                           
      30 +            names = [m["name"] for m in json.load(response)["mo
         +dels"]]                                                        
      31 +    except OSError:                                            
      32 +        pytest.skip(f"Ollama is not reachable at {ENDPOINT}")  
      33 +    return {*names, *(n.split(":")[0] for n in names)}         
      34 +                                                               
      35 +                                                               
      36 +@pytest.fixture()                                              
      37 +def service(tmp_path, monkeypatch):                            
      38 +    if EMBED_MODEL not in installed():                         
      39 +        pytest.skip(f"embedding model {EMBED_MODEL} is not pull
         +ed")                                                           
      40 +    monkeypatch.setenv("METIS_EMBED_ENGINE", "local")          
     … +54 lines (ctrl+o to expand)
  ⎿  (timeout 5m)

⏺ Decomposition and the queue fixes are built, but neither decomposition nor 
  semantic search has run on real Ollama yet. Ollama isn't installed on this
  machine, so both live tests skip. The steps are in
  ops/ollama-rag-verification.md.

  The work is on branch rag-decomposition (0e9f4c3, 18f9d01), not merged.
  Python: 620 passed, 1 skipped, and the 2 live Ollama tests are excluded from
  that run. All 129 web tests pass, and ruff, mypy and the contracts check are
  clean.

  What's built

  - Parallel decomposition: sub-questions run at once alongside the original
    query, then merge with RRF.
  - Sequential decomposition: each step's answer feeds the next query. If a step
    finds nothing, it stops and says so instead of guessing.
  - Router and fallback: simple queries skip the LLM. If the model fails, you
    get normal hybrid search with a note.
  - Turning it on: it's off by default. Set METIS_QUERY_DECOMPOSITION=local and
    METIS_LOCAL_LLM_MODEL. The API returns the mode and sub-queries, but the
    Search screen doesn't show them yet.
  - Unsupported binaries: images, databases and similar files are skipped.
    Scanned PDFs are marked skipped with a reason, and only malformed files
    reach the DLQ.
  - FIFO dead-letter queue: oldest entries come first and expire after 30 days
    or past 200 per user. Expired entries can't be retried but stay in the
    database for audit.
  - Lingering dead letters: an entry clears itself once the file is fixed and
    indexed, or deleted.
  - Cloud gap: all of this is SQLite only; the DynamoDB store has no dead-letter
    retry yet.

  Tauri latency

  I measured the running app but did not reproduce the blank window.

  ┌──────────────┬──────────────────────────┬──────────────────────────────┐
  │   Finding    │       Measurement        │            Effect            │
  ├──────────────┼──────────────────────────┼──────────────────────────────┤
  │ Running app  │ Built Oct 7              │ Has none of the wave50 work  │
  │ is stale     │                          │                              │
  ├──────────────┼──────────────────────────┼──────────────────────────────┤
  │ Slow sidecar │ 6.5–9.8s per launch, 102 │ Likely main startup delay;   │
  │  startup     │  MB --onefile binary     │ three services start this    │
  │              │                          │ way                          │
  ├──────────────┼──────────────────────────┼──────────────────────────────┤
  │ UI waits for │ DesktopGate blocks the   │ Only "Starting local         │
  │  the API     │ app                      │ services…" shows until then  │
  ├──────────────┼──────────────────────────┼──────────────────────────────┤
  │ Memory       │ 14.2 GB of 15.4 GB swap  │ Idle app pages out, so it    │
  │ pressure     │ used                     │ can feel slow on return      │
  ├──────────────┼──────────────────────────┼──────────────────────────────┤
  │ Worker       │ Every 2s, 4s when idle   │ Adds delay to each           │
  │ polling      │                          │ background job               │
  ├──────────────┼──────────────────────────┼──────────────────────────────┤
  │ API itself   │ 1–4 ms                   │ Not the bottleneck           │
  └──────────────┴──────────────────────────┴──────────────────────────────┘

  The startup screen renders without the backend, so a window that stays fully 
  blank points to JavaScript failing to load or crashing, as your note suggests.
  A long "Starting local services…" points to the sidecar.

  The other agents: metis-d1 confirmed no overlap with the queue functions. I
  haven't coordinated with Codex, and someone else is committing to
  dlq-idempotency, so I stayed in my own worktree.

  When you open Metis, does the window stay blank forever, or does the UI appear
  after several seconds? If it appears, I'll switch the sidecar to --onedir and
  paint a static shell immediately. If it stays blank, I'll rebuild current
  code and read the web inspector's console.

✻ Crunched for 10m 49s · done 2:34 PM

❯ continue                                                                      

⏺ Going ahead without the answer: I'll fix first paint and sidecar startup,
  since those help in both cases. First I'm measuring how much --onedir actually
  saves.

  Ran 2 shell commands (ctrl+o to expand)

⏺ --onedir cuts API startup from at least 6.5s to about 0.5–1.7s, so I'm wiring
  it into the real bundle.

⏺ Bash(cd
      /Users/nityanthmaramreddy/Downloads/Metis/.worktrees/rag-decomposition;
      grep -rn 'binaries\|externalBin\|onefile' apps/desktop --include='*.rs'
      --include='*.…)
  ⎿  apps/desktop/src-tauri/tests/capture_e2e.rs:61:    let dir = PathBuf::
     from(env!("CARGO_MANIFEST_DIR")).join("binaries");
     apps/desktop/src-tauri/tauri.conf.json:26:    "externalBin": ["binarie
     … +32 lines (ctrl+o to expand)
  ⎿  Updated
     .worktrees/rag-decomposition/apps/desktop/python-backend/build.sh +8
     -6
      1  #!/bin/sh
      2 -# Freeze the Metis services into ONE executable: src-tauri/binar
        -ies/metis-backend-<triple>.                                     
      2 +# Freeze the Metis services into src-tauri/binaries/metis-backen
        +d/ (one executable + libraries).                                
      3 +# --onedir on purpose: --onefile re-extracts ~250 MB on every la
        +unch of all three services.                                     
      4  # Needs uv + network for PyInstaller on first run. Never downloa
         ds model weights.
      5  # Env: METIS_BACKEND_ASR=1 (default) bundles faster-whisper if i
         t resolves;
      6  #      METIS_BACKEND_ASR=0 builds without it (speech then report
         s `failed` honestly).
     ...
      10  out="$here/../src-tauri/binaries"
      11  work="$here/.build"
      12  export PATH="$HOME/.cargo/bin:$PATH"
      12 -triple=$(rustc -vV | sed -n 's/^host: //p')                    
      13 -ext=""; case "$triple" in *windows*) ext=".exe";; esac         
      13 +case "$(rustc -vV | sed -n 's/^host: //p')" in *windows*) ext="
         +.exe";; *) ext="";; esac                                       
      14  asr="${METIS_BACKEND_ASR:-1}"
      15  
      16  mkdir -p "$out" "$work"
     ...
      29  if [ "$asr" = "1" ]; then
      30    collect="--collect-all faster_whisper --collect-all ctranslat
          e2 --collect-all av --collect-all tokenizers"
      31  fi
      32 -"$work/venv/bin/python" -m PyInstaller --noconfirm --clean --on
         -efile \                                                        
      32 +"$work/venv/bin/python" -m PyInstaller --noconfirm --clean --on
         +edir \                                                         
      33    --name "metis-backend" --distpath "$work/dist" --workpath "$w
          ork/pyi" --specpath "$work" \
      34    --paths "$here" \
      35    --collect-all metis_api --collect-all metis_pipeline --collec
          t-all metis_state \
     ...
      37    --collect-all metis_speech --collect-all metis_contracts --co
          llect-submodules uvicorn \
      38    --hidden-import uvloop --hidden-import httptools --hidden-imp
          ort websockets \
      39    $collect "$here/metis_backend.py"
      40 -cp "$work/dist/metis-backend$ext" "$out/metis-backend-$triple$e
         -xt"                                                            
      41 -echo "built $out/metis-backend-$triple$ext (asr=$asr)"         
      40 +rm -rf "$out/metis-backend"                                    
      41 +cp -R "$work/dist/metis-backend" "$out/metis-backend"          
      42 +touch "$out/metis-backend/metis-backend$ext"  # prepare-sidecar
         + compares sources to this                                      
      43 +echo "built $out/metis-backend/metis-backend$ext (asr=$asr)"   
  ⎿  Updated
     .worktrees/rag-decomposition/apps/desktop/scripts/prepare-sidecar.sh
     +7 -6
       1  #!/bin/sh
       2  # Make the workspace buildable and testable:
       3 -#  - src-tauri/binaries/metis-backend-<triple>: the REAL packag
         -ed backend that                                                
       4 -#    Tauri's externalBin requires. Built once (slow, needs uv +
         - network for                                                   
       5 -#    PyInstaller) and reused; delete it or run python-backend/b
         -uild.sh to rebuild.                                            
       3 +#  - src-tauri/binaries/metis-backend/: the REAL packaged backe
         +nd, bundled as a                                               
       4 +#    Tauri resource. Rebuilt (slow, needs uv + network for PyIn
         +staller) when it                                               
       5 +#    is missing or any Python source is newer, so an app never 
         +ships stale code.                                              
       6  #  - target/debug/metis-health-sidecar: test-only fake backend 
          (never bundled).
       7  set -eu
       8  cd "$(dirname "$0")/.."
       9  export PATH="$HOME/.cargo/bin:$PATH"
      10 -triple=$(rustc -vV | sed -n 's/^host: //p')                    
      11 -ext=""; case "$triple" in *windows*) ext=".exe";; esac         
      12 -if [ ! -f "src-tauri/binaries/metis-backend-$triple$ext" ]; the
         -n                                                              
      10 +case "$(rustc -vV | sed -n 's/^host: //p')" in *windows*) ext="
         +.exe";; *) ext="";; esac                                       
      11 +exe="src-tauri/binaries/metis-backend/metis-backend$ext"       
      12 +if [ ! -f "$exe" ] || [ -n "$(find ../../services ../../package
         +s/contracts/src python-backend \                               
      13 +  -name '*.py' -not -path '*/.build/*' -newer "$exe" -print -qu
         +it)" ]; then                                                   
      14    sh python-backend/build.sh
      15  fi
      16  cargo build -p metis-health-sidecar
  ⎿  Updated .worktrees/rag-decomposition/apps/desktop/src-tauri/src/lib.rs
     +5 -6
      217      )
      218  }
      219  
      220 -fn start_stack(data_dir: PathBuf) -> Result<Stack, String> {  
      221 -    let backend = std::env::current_exe()                     
      222 -        .map_err(|e| e.to_string())?                          
      223 -        .parent()                                             
      224 -        .ok_or("no executable directory")?                    
      220 +fn start_stack(resources: PathBuf, data_dir: PathBuf) -> Resul
          +t<Stack, String> {                                            
      221 +    let backend = resources                                   
      222 +        .join("backend")                                      
      223          .join(format!("{BACKEND}{}", std::env::consts::EXE_SUF
           FIX));
      224      let mut cfg = Config::production(backend, &data_dir);
      225      cfg.local_ai = LocalAi::from_env()?;
     ...
      283          .setup(|app| {
      284              let state = app.state::<AppState>();
      285              let data_dir = app.path().app_data_dir().map_err(|
           e| e.to_string());
      288 -            let started = data_dir.clone().and_then(start_stac
          -k);                                                           
      286 +            let resources = app.path().resource_dir().map_err(
          +|e| e.to_string());                                           
      287 +            let started = resources.and_then(|r| data_dir.clon
          +e().and_then(|d| start_stack(r, d)));                         
      288              match started {
      289                  Ok(stack) => *state.stack.lock().unwrap() = So
           me(stack),
      290                  Err(e) => *state.startup_error.lock().unwrap()
            = Some(e),
  ⎿  Updated
     .worktrees/rag-decomposition/apps/desktop/src-tauri/tauri.conf.json +1
     -1
      23      "active": true,
      24      "targets": ["app", "dmg", "msi", "nsis", "deb", "appimage"]
          ,
      25      "icon": ["icons/icon.png"],
      26 -    "externalBin": ["binaries/metis-backend"],                 
      26 +    "resources": { "binaries/metis-backend/": "backend/" },    
      27      "macOS": { "minimumSystemVersion": "11.0" }
      28    }
      29  }
  ⎿  Updated .worktrees/rag-decomposition/apps/web/index.html +3 -1
       8      <link rel="preconnect" href="https://fonts.googleapis.com" 
          />
       9      <link href="https://fonts.googleapis.com/css2?family=Inter:
          wght@400;500;600&display=swap" rel="stylesheet" />
      10      <title>Metis</title>
      11 +    <style>:root{color-scheme:light dark}body{margin:0;backgrou
         +nd:#fff}@media (prefers-color-scheme:dark){body{background:#0a0
         +a0a}}</style>                                                  
      12    </head>
      13    <body>
      13 -    <div id="root"></div>                                      
      14 +    <!-- Painted before any JavaScript runs; React replaces it 
         +on mount. -->                                                  
      15 +    <div id="root"><main role="status" style="min-height:100dvh
         +;display:grid;place-items:center;font:14px system-ui,sans-serif
         +;color:#737373">Starting Metis…</main></div>                   
      16      <script type="module" src="/src/main.tsx"></script>
      17    </body>
      18  </html>
  ⎿  (timeout 10m)

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis/.worktrees/rag-decomposition
      /apps/desktop/src-tauri; sed -n 55,80p tests/capture_e2e.rs; sed -n 6,40p
      tests/real_ba…)
  ⎿      fn delete(&self, i: &str) {
             self.0.delete(i)
         }
     … +102 lines (ctrl+o to expand)
  ⎿  Updated .worktrees/rag-decomposition/apps/desktop/src-tauri/src/lib.rs
     +1 -1
      21  /// Event the HUD listens to; payload is a sanitized [`Snapshot
          `].
      22  pub const STATE_EVENT: &str = "capture:state";
      23  
      24 -/// Name of the packaged backend (matches `bundle.externalBin`)
         -.                                                              
      24 +/// Name of the packaged backend (under the `backend/` bundle r
         +esource).                                                      
      25  const BACKEND: &str = "metis-backend";
      26  
      27  #[derive(Default)]
  ⎿  Updated
     .worktrees/rag-decomposition/apps/desktop/src-tauri/src/stack.rs +1 -1
      201              jitter: 0.3,
      202              max_restarts: 5,
      203              stable_after: Duration::from_secs(60),
      204 -            // A cold onefile executable unpacks and imports f
          -or several seconds.                                           
      204 +            // A cold start imports for a second or more; slow
          +er under memory pressure.                                     
      205              startup_deadline: Duration::from_secs(60),
      206              warm_deadline: Duration::from_secs(180),
      207              health_interval: Duration::from_secs(5),
  ⎿  Updated .worktrees/rag-decomposition/apps/desktop/src-tauri/tests/capt
     ure_e2e.rs +1 -1
      58  }
      59  
      60  fn backend() -> PathBuf {
      61 -    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("b
         -inaries");                                                     
      61 +    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("b
         +inaries").join("metis-backend");                               
      62      std::fs::read_dir(&dir)
      63          .unwrap_or_else(|_| panic!("{} missing: run `pnpm sidec
          ar:prepare`", dir.display()))
      64          .flatten()
  ⎿  Updated .worktrees/rag-decomposition/apps/desktop/src-tauri/tests/real
     _backend.rs +2 -2
       7  use std::time::{Duration, Instant};
       8  
       9  fn backend() -> PathBuf {
      10 -    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("b
         -inaries");                                                     
      10 +    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("b
         +inaries").join("metis-backend");                               
      11      let triple_suffix = "metis-backend-".to_string();
      12      std::fs::read_dir(&dir)
      13          .unwrap_or_else(|_| panic!("{} missing: run `pnpm sidec
          ar:prepare`", dir.display()))
     ...
      33      let dir = std::env::temp_dir().join(format!("metis-real-{}"
          , std::process::id()));
      34      let mut stack = Stack::start(Config::production(backend(), 
          &dir)).unwrap();
      35  
      36 -    // Cold PyInstaller onefile start-up is several seconds.   
      36 +    // Cold PyInstaller start-up can take seconds under memory 
         +pressure.                                                      
      37      let end = Instant::now() + Duration::from_secs(120);
      38      let st = loop {
      39          let st = stack.status();

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis/.worktrees/rag-decomposition
      /apps/desktop
      python3 - <<'PY'…)
  ⎿  fmt-ok
     test result: ok. 60 passed; 0 failed; 0 ignored; 0 measured; 0 filtere
     d out; finished in 0.57s
     … +16 lines (ctrl+o to expand)
  ⎿  Updated .worktrees/rag-decomposition/apps/desktop/src-tauri/tests/capt
     ure_e2e.rs +9 -12
      58  }
      59  
      60  fn backend() -> PathBuf {
      61 -    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("b
         -inaries").join("metis-backend");                               
      62 -    std::fs::read_dir(&dir)                                    
      63 -        .unwrap_or_else(|_| panic!("{} missing: run `pnpm sidec
         -ar:prepare`", dir.display()))                                  
      64 -        .flatten()                                             
      65 -        .map(|e| e.path())                                     
      66 -        .find(|p| {                                            
      67 -            p.file_name()                                      
      68 -                .unwrap()                                      
      69 -                .to_string_lossy()                             
      70 -                .starts_with("metis-backend-")                 
      71 -        })                                                     
      72 -        .expect("packaged backend missing: run `pnpm sidecar:pr
         -epare`")                                                       
      61 +    let exe = PathBuf::from(env!("CARGO_MANIFEST_DIR"))        
      62 +        .join("binaries/metis-backend")                        
      63 +        .join(format!("metis-backend{}", std::env::consts::EXE_
         +SUFFIX));                                                      
      64 +    assert!(                                                   
      65 +        exe.is_file(),                                         
      66 +        "{} missing: run `pnpm sidecar:prepare`",              
      67 +        exe.display()                                          
      68 +    );                                                         
      69 +    exe                                                        
      70  }
      71  
      72  /// 1.5 s, 48 kHz stereo, 16-bit: exercises downmix + resample 
          on the way in.
  ⎿  Updated .worktrees/rag-decomposition/apps/desktop/src-tauri/tests/real
     _backend.rs +9 -11
       7  use std::time::{Duration, Instant};
       8  
       9  fn backend() -> PathBuf {
      10 -    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("b
         -inaries").join("metis-backend");                               
      11 -    let triple_suffix = "metis-backend-".to_string();          
      12 -    std::fs::read_dir(&dir)                                    
      13 -        .unwrap_or_else(|_| panic!("{} missing: run `pnpm sidec
         -ar:prepare`", dir.display()))                                  
      14 -        .flatten()                                             
      15 -        .map(|e| e.path())                                     
      16 -        .find(|p| {                                            
      17 -            let n = p.file_name().unwrap().to_string_lossy().to
         -_string();                                                     
      18 -            n.starts_with("metis-backend-") && n.starts_with(&t
         -riple_suffix)                                                  
      19 -        })                                                     
      20 -        .expect("packaged backend missing: run `pnpm sidecar:pr
         -epare`")                                                       
      10 +    let exe = PathBuf::from(env!("CARGO_MANIFEST_DIR"))        
      11 +        .join("binaries/metis-backend")                        
      12 +        .join(format!("metis-backend{}", std::env::consts::EXE_
         +SUFFIX));                                                      
      13 +    assert!(                                                   
      14 +        exe.is_file(),                                         
      15 +        "{} missing: run `pnpm sidecar:prepare`",              
      16 +        exe.display()                                          
      17 +    );                                                         
      18 +    exe                                                        
      19  }
      20  
      21  fn open(port: u16) -> bool {
  ⎿  (timeout 8m 20s)

  Ran 2 shell commands (ctrl+o to expand)

⏺ Desktop startup is fixed: the local API now becomes healthy in 1.4–1.8s.
  Before, the sidecar took 6.5–9.8s just to unpack, before it could serve
  anything. I launched the new build three times with an isolated data folder,
  but I haven't seen its window, so the blank-window bug isn't confirmed gone.

  It's committed on rag-decomposition (d2f6c49), not merged. Rust tests, clippy,
  fmt, the web build and all 129 web tests pass.

  What changed

  - Sidecar packaging: switched from PyInstaller --onefile to --onedir, so the
    three services no longer unpack the backend on every launch.
  - First paint: index.html now shows "Starting Metis…" before any JavaScript
    runs. If the window is still fully blank, the webview isn't loading the page
    at all.
  - Stale backend bug: the build reused any existing sidecar, so a rebuilt app
    could ship old Python. The sidecar on disk predated the wave50 merge. It now
    rebuilds whenever Python sources change.

  Tradeoffs and remaining issues

  - Bigger app: the debug bundle is now 360 MB, because the backend ships
    unpacked at 329 MB.
  - Release signing: only tested with ad-hoc signing. Notarizing the nested
    libraries still needs a check, and Windows/Linux packaging is untested.
  - Slow exit after a forced kill: sidecars took up to about 30s to exit after I
    sent the app SIGTERM. They did exit on their own.
  - Worker polling: still adds 2–4s per background job. I left it alone because
    another agent is working on the queue.
  - Memory pressure: swap was nearly full (14.2 of 15.4 GB), which will still
    slow the app after it sits idle.
  - Ollama: semantic search and decomposition remain unverified, as noted in
    ops/ollama-rag-verification.md.

  Try it

  Your open Metis is still the Oct 7 build and shares the same data folder, so
  quit it first, then run:

  open "/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/rag-decomposition/a
  pps/desktop/target/debug/bundle/macos/Metis.app"

  If it opens to the app within a couple of seconds, startup is solved. If it
  sticks on "Starting Metis…" or stays blank, right-click the window, choose
  Inspect, and send me the Console errors.

✻ Churned for 8m 54s · done 8:51 PM

❯ zsh: permission denied: /Users/nityanthmaramreddy/Downloads/Metis/.worktrees/ 
  rag-decomposition/apps/desktop/target/debug/bundle/macos/Metis.app (take      
  careof this                                                                   

  Ran 1 shell command (ctrl+o to expand)

⏺ The new Metis build is open now. The error came from running the .app path
  without open: a .app is a folder, so zsh can't execute it.

  Your old Metis (Oct 7 build) is still running, and I left it alone because it
  may have work in progress. Two copies on the same database could conflict, so
  I launched the new one with a separate, empty data folder. It won't show your
  existing tasks or notes, and anything you enter there stays in that test copy.

  The process is running, but macOS blocked me from reading its window, so I
  can't confirm what it's showing. Look for the newest Metis window:
  - App appears within a couple of seconds: the startup fix works.
  - Stuck on "Starting Metis…" or blank: right-click, choose Inspect, and send
    me the Console errors.

  To run it on your real data, quit both Metis windows, then paste this whole
  line:

  open "/Users/nityanthmaramreddy/Downloads/Metis/.worktrees/rag-decomposition/a
  pps/desktop/target/debug/bundle/macos/Metis.app"

✻ Brewed for 26s · done 9:29 PM

❯ Not ready to merge yet, because two branches have uncommitted work:           
  - dlq-idempotency has 1 uncommitted file in your main checkout.               
  - rag-decomposition has 8 uncommitted files in its worktree.                  
    commit this all now                                                         

  Ran 2 shell commands (ctrl+o to expand)

⏺ Everything is committed, and both branches are clean.

  - dlq-idempotency: committed the one uncommitted file, GCP.md, as f12f980.
  - rag-decomposition: nothing to commit. Its work was already in d2f6c49, so
    the 8 uncommitted files in your note were likely from before that commit.

  A trial merge of the two branches shows no conflicts. rag-decomposition is 11
  commits behind dlq-idempotency, and I haven't run the tests on the combined
  code. Nothing has been merged or pushed.

  One thing to fix before merging: the GCP.md commit includes the blank-window
  troubleshooting text you pasted earlier, added at the bottom of the operator
  guide. It looks accidental. I committed it as is because you asked for
  everything, and a scan of the added lines found no credentials.

  Want me to remove that section, merge rag-decomposition into dlq-idempotency,
  and run the full test suite?

✻ Churned for 22s · done 9:32 PM

❯ merge it all. and yes that remove section. and then run the full test suite   

⏺ Bash(cd /Users/nityanthmaramreddy/Downloads/Metis; git branch --show-current;
      git status --short; git log --oneline -3; n=$(grep -n '^Since it’s the
      \*\*built app' G…)
  ⎿  dlq-idempotency
     f12f980 docs(gcp): format operator guide and add blank-window notes
     3edc3c4 ops: protect VM data from accidental deletion
     … +31 lines (ctrl+o to expand)
  ⎿  Updated GCP.md -16
  ⎿  Updated apps/desktop/python-backend/build.sh +8 -6
  ⎿  Updated apps/desktop/scripts/prepare-sidecar.sh +7 -6
  ⎿  Updated apps/desktop/src-tauri/src/lib.rs +6 -7
  ⎿  Updated apps/desktop/src-tauri/src/stack.rs +1 -1
  ⎿  … +24 files changed
     apps/desktop/src-tauri/tauri.conf.json
     apps/desktop/src-tauri/tests/capture_e2e.rs
     apps/desktop/src-tauri/tests/real_backend.rs
     +21 more
  ⎿  (a git step in this command can move the working tree, so these may be
     its changes, not edits) (ctrl+o to expand)

⏺ Merged. Now running the full suite: Python, web and desktop.

  Ran 2 shell commands (ctrl+o to expand)

⏺ Merged, the pasted section is gone, and every test that ran passed. One lint 
  error remains, and it isn't from this work. rag-decomposition is now in
  dlq-idempotency (628acff). I did not merge into main or push.

  ┌──────────────────────────────┬───────────────────────────────────────┐
  │            Check             │                Result                 │
  ├──────────────────────────────┼───────────────────────────────────────┤
  │ Python tests                 │ 622 passed, 1 skipped                 │
  ├──────────────────────────────┼───────────────────────────────────────┤
  │ Desktop sidecar tests        │ 2 passed                              │
  ├──────────────────────────────┼───────────────────────────────────────┤
  │ Rust tests                   │ 73 passed; clippy and fmt clean       │
  ├──────────────────────────────┼───────────────────────────────────────┤
  │ Web                          │ 129 passed; typecheck and build clean │
  ├──────────────────────────────┼───────────────────────────────────────┤
  │ mypy, ruff format, contracts │ Clean                                 │
  ├──────────────────────────────┼───────────────────────────────────────┤
  │ Ruff lint                    │ 1 error                               │
  ├──────────────────────────────┼───────────────────────────────────────┤
  │ Integration tests            │ 4 skipped, none ran                   │
  └──────────────────────────────┴───────────────────────────────────────┘

  - Lint error: ops/gcp/monitoring/queue-observer.py:53 uses print (T201). It
    came in with the GCP monitoring commits rather than the RAG branch, so I
    left it for whichever agent owns that script. It's probably intentional
    output and may just need a lint exemption.
  - Skipped integration tests: 2 need Ollama running, so semantic search and
    decomposition are still unverified on a real model. The other 2 need the asr
    extra for audio decoding.
  - Desktop window: still not visually confirmed. Rebuilding the sidecar here
    doesn't change your running app, so the Oct 7 build stays open until you
    quit it.

  Should I merge dlq-idempotency into main now? I'd wait until the queue and
  Codex agents finish, since someone else has been committing to this branch in
  the same checkout.

✻ Baked for 5m 29s · done 9:40 PM