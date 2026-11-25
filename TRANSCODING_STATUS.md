# Video Transcoding Status & Plan

**Letzte Aktualisierung:** 2025-11-29 18:50

## ✅ CODE GESICHERT!

**Status:** Der funktionierende Code wurde erfolgreich ins Repository committed!

**Commit:** `7d4d490` - "fix: add /transcode-sync endpoint with proper ZIP cleanup"
**Branch:** `dev`
**Repository:** `mac_transcoding_api`
**Datum:** 2025-11-29 18:48

**Geänderte Dateien:**
- `main.py` (+101 Zeilen) - `/transcode-sync` Endpoint hinzugefügt
- `transcoding/remote.py` (+17 Zeilen, -4 Zeilen) - Verwendet jetzt `/transcode-sync`

**GitHub:** https://github.com/apopovic77/mac-transcoding-api/tree/dev

---

## ZIEL (Was der User will)

**DISTRIBUTED TRANSCODING MIT ZIP MODE:**
- Videos sollen zur **transcoding-api** (remote) gesendet werden
- Die transcoding-api soll **ZIP files** zurückgeben (nicht paths)
- arkserver und arkturian.com sollen die transcoding-api nutzen können

**NICHT gewünscht:** LOCAL transcoding mit ffmpeg direkt auf dem Server!

---

## AKTUELLER STATUS

### arkserver Configuration

**Location:** `/var/www/api-storage.arkturian.com/.env`
```bash
TRANSCODING_MODE=remote              # Sendet zu transcoding-API
TRANSCODING_RESPONSE_MODE=path       # TODO: Auf "zip" ändern
TRANSCODING_API_URL=http://localhost:8082
```

**Celery Worker Service:** `/etc/systemd/system/celery-worker.service`
```bash
Environment="TRANSCODING_MODE=remote"           # HARDCODED in systemd!
Environment="TRANSCODING_RESPONSE_MODE=path"    # HARDCODED in systemd!
Environment="TRANSCODING_API_URL=http://localhost:8082"
```

⚠️ **WICHTIG:** Die systemd service file überschreibt die .env Datei!

### arkturian.com Configuration

**Location:** `/var/www/api-storage.arkturian.com/.env`
```bash
TRANSCODING_MODE=remote                              ✅
TRANSCODING_RESPONSE_MODE=zip                        ✅ (bereits korrekt!)
TRANSCODING_API_URL=http://arkserver.arkturian.com:8082  ✅
```

**Celery Worker Service:** `/etc/systemd/system/celery-worker.service`
```bash
# TODO: Service file checken ob hardcoded values vorhanden
```

**Status:** ✅ Konfiguration ist bereits korrekt für distributed ZIP transcoding!

### Transcoding API (arkserver)

**Service:** `transcoding-api.service`
**Port:** 8082
**Location:** `/var/www/transcoding-api/`
**Endpoints:**
- `GET /` - Health check
- `GET /health` - Health check mit ffmpeg test
- `POST /transcode` - Transcoding endpoint (multipart form-data)

---

## AKTUELLES PROBLEM

**500 Internal Server Error** von transcoding-API beim `/transcode` endpoint.

### Fehler-Logs
```
Nov 29 17:41:09 - POST /transcode HTTP/1.1 500 Internal Server Error
ERROR: Exception in ASGI application
```

**Ursache:** Unbekannt - vollständiger Stacktrace fehlt noch.

### Vermutete Ursachen:
1. Falsches Request-Format von storage-api zu transcoding-api
2. Missing parameters im /transcode endpoint
3. File upload format issue
4. Transcoding package import fehlt

---

## WAS BEREITS GETESTET WURDE (nicht funktioniert)

1. ❌ TRANSCODING_MODE=local (war NIE gewünscht!)
2. ❌ .env ohne systemd service update (wird überschrieben)
3. ❌ Verschiedene Test-Videos mit falschem MIME type

---

## NÄCHSTE SCHRITTE (Klarer Plan)

### 1. Vollständigen Error von transcoding-API herausfinden
```bash
ssh root@arkserver "journalctl -u transcoding-api --since '10 minutes ago' --no-pager | tail -100"
```

### 2. Transcoding-API Code überprüfen
- Wo ist der /transcode endpoint definiert?
- Welche Parameter erwartet er?
- Ist das Request-Format korrekt?

**File:** `/var/www/transcoding-api/main.py` (oder `/tmp/transcoding_api_server.py`)

### 3. Storage-API Remote Transcoder Code überprüfen
- Wie ruft storage-api die transcoding-API auf?
- Sendet sie die File korrekt als multipart/form-data?

**File:** `/var/www/api-storage.arkturian.com/storage/transcoding_helper.py`
**Methode:** `RemoteTranscoder.transcode()` (aus transcoding package)

### 4. 500 Error fixen

Basierend auf gefundenen Fehler:
- Request format anpassen
- Missing parameters hinzufügen
- Endpoint-Signatur korrigieren

### 5. ZIP Mode aktivieren

Nach erfolgreichem transcoding:
```bash
# In .env UND systemd service file:
TRANSCODING_RESPONSE_MODE=zip

# Service neu laden:
systemctl daemon-reload
systemctl restart celery-worker
```

### 6. Test durchführen

```bash
# Test video hochladen:
ssh root@arkserver 'curl -X POST "https://api-storage.arkserver.arkturian.com/storage/upload" \
  -H "X-API-KEY: $STORAGE_API_KEY" \
  -F "file=@/tmp/test-video.mp4;type=video/mp4" \
  -F "title=ZIP Mode Test" \
  -F "ai_mode=safety"'

# Celery logs checken:
ssh root@arkserver "tail -f /var/log/celery/worker.log"

# Transcoding-API logs checken:
ssh root@arkserver "journalctl -u transcoding-api -f"
```

### 7. Verify Success

**Success Criteria:**
- ✅ Kein 500 error
- ✅ `transcoding_status: "completed"`
- ✅ HLS files existieren
- ✅ `hls_url` ist gesetzt

---

## WICHTIGE DATEIEN & LOCATIONS

### arkserver

**Storage API:**
- Code: `/var/www/api-storage.arkturian.com/`
- Config: `/var/www/api-storage.arkturian.com/.env`
- Helper: `/var/www/api-storage.arkturian.com/storage/transcoding_helper.py`
- Celery Service: `/etc/systemd/system/celery-worker.service`
- Celery Logs: `/var/log/celery/worker.log`

**Transcoding API:**
- Code: `/var/www/transcoding-api/`
- Main: `/var/www/transcoding-api/main.py` (oder `/tmp/transcoding_api_server.py`)
- Service: `/etc/systemd/system/transcoding-api.service`
- Logs: `journalctl -u transcoding-api`
- Port: 8082

**Uploaded Files:**
- Path: `/mnt/backup-disk/uploads/storage/media/arkturian/`
- Transcoded: `/mnt/backup-disk/uploads/storage/media/arkturian/{basename}_transcoded/`

### arkturian.com

**TODO:** Noch zu konfigurieren für distributed transcoding.

---

## TRANSCODING FLOW

```
1. User uploads video
   ↓
2. storage-api creates StorageObject
   ↓
3. enqueue_ai_safety_and_transcoding() queues Celery task
   ↓
4. Celery task: process_video_transcoding
   ↓
5. TranscodingHelper.transcode_video_sync()
   ↓
6. Creates TranscodingConfig with mode=remote
   ↓
7. TranscoderFactory.create() → RemoteTranscoder
   ↓
8. RemoteTranscoder.transcode()
   ↓
9. POST to http://localhost:8082/transcode
   ↓
10. transcoding-api processes video
   ↓
11. Returns ZIP (wenn RESPONSE_MODE=zip)
   ↓
12. storage-api extracts ZIP
   ↓
13. Updates DB: transcoding_status="completed", hls_url=...
```

---

## COMMANDS REFERENCE

### Check Services
```bash
ssh root@arkserver "systemctl status storage-api"
ssh root@arkserver "systemctl status celery-worker"
ssh root@arkserver "systemctl status transcoding-api"
```

### Check Logs
```bash
ssh root@arkserver "journalctl -u storage-api -f"
ssh root@arkserver "tail -f /var/log/celery/worker.log"
ssh root@arkserver "journalctl -u transcoding-api -f"
```

### Restart Services
```bash
ssh root@arkserver "systemctl restart celery-worker"
ssh root@arkserver "systemctl restart storage-api"
ssh root@arkserver "systemctl restart transcoding-api"
```

### Test Endpoints
```bash
# Transcoding API health:
curl http://localhost:8082/health

# Upload test video:
curl -X POST "https://api-storage.arkserver.arkturian.com/storage/upload" \
  -H "X-API-KEY: $STORAGE_API_KEY" \
  -F "file=@/tmp/test.mp4;type=video/mp4" \
  -F "title=Test" \
  -F "ai_mode=safety"
```

---

## TRANSCODING PACKAGE

Das `transcoding` Python Package wird von storage-api verwendet.

**Installation:**
```bash
pip install -e /path/to/mac_transcoding_api
```

**Classes:**
- `TranscodingConfig` - Configuration
- `TranscodingMode` - Enum: LOCAL, REMOTE
- `TranscoderFactory` - Factory für LocalTranscoder oder RemoteTranscoder
- `RemoteTranscoder` - Sendet zu transcoding-API via HTTP
- `LocalTranscoder` - Verwendet ffmpeg lokal

---

## DEBUGGING CHECKLIST

Wenn transcoding nicht funktioniert:

- [ ] Ist ffmpeg installiert? `ffmpeg -version`
- [ ] Läuft transcoding-api? `systemctl status transcoding-api`
- [ ] Ist Port 8082 erreichbar? `curl http://localhost:8082/health`
- [ ] Sind Environment Variables korrekt? `systemctl show celery-worker | grep Environment`
- [ ] Gibt es 500 errors in transcoding-API logs? `journalctl -u transcoding-api -f`
- [ ] Ist MIME type korrekt? Muss `video/*` sein
- [ ] Wurde Celery nach Config-Änderung neu gestartet?
- [ ] Wurde systemd daemon-reload ausgeführt nach service file Änderung?

---

## BEKANNTE ISSUES

1. **systemd service überschreibt .env:**
   - Environment variables in `/etc/systemd/system/celery-worker.service` haben Vorrang
   - Nach Änderung: `systemctl daemon-reload && systemctl restart celery-worker`

2. **MIME type detection:**
   - curl muss explicit `type=video/mp4` setzen
   - Sonst: `application/octet-stream` → kein transcoding

3. **500 errors von transcoding-API:**
   - Noch zu debuggen
   - Vollständiger stacktrace fehlt

---

## NEXT SESSION START HERE

1. Lies diese Datei komplett
2. Check aktuellen Status mit commands oben
3. Fahre bei "NÄCHSTE SCHRITTE" fort
4. Update diese Datei mit neuen Findings

**NICHT MEHR VERSUCHEN:**
- ❌ LOCAL mode (nicht gewünscht!)
- ❌ .env ohne systemd update (funktioniert nicht!)
