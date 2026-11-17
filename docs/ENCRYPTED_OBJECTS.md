# Verschlüsselte Storage-Objekte (#3230)

Konzept und Freigabe: Content-Beitrag 6192, Produkt `p-bb4917ce5da0` (approved 17.11.2026, Cloud-Security + Storage).
Dieses Dokument beschreibt den Code und den Betrieb. Der Code ist **ohne Konfiguration wirkungslos**: Solange
`STORAGE_ENCRYPTION_BACKEND` und der Notfallschlüssel fehlen, antwortet jeder Upload mit `encrypted=true` mit
`503 encryption_key_unavailable`, alles andere läuft wie bisher.

## Schnittstelle

| Weg | Wie |
|---|---|
| Multipart | `POST /storage/upload` mit `encrypted=true` (Form), optional `grant_scope`, `ttl_hours` |
| Ticket | `POST /storage/upload-ticket?encrypted=true` mit Header `X-Filename: <percent-encoded UTF-8>`; `filename` in der URL wird mit 400 abgelehnt |
| Chunks | `POST /storage/upload-chunk?...&encrypted=true` mit `X-Filename` |
| Lesen | `GET /storage/media/{id}`: Eigentümer oder signierter Link, ein Range-Bereich, `HEAD`, `If-None-Match` |
| Signieren | `POST /storage/sign` deckelt verschlüsselte Objekte auf 300 s (`ttl: 300`, `encrypted: true` im Item) |
| Ersetzen | `PUT /storage/files/{id}`: frischer DEK, Name bleibt |

Abgelehnt wird mit 409: Varianten und Formatwechsel (`encrypted_no_variants`), sprechende URL
`/media/{id}/{name.ext}` (`encrypted_no_named_url`), `GET /files/{id}` (`encrypted_use_media`), Analyse, Embedding,
KG-Embed, Data-URI, Bild ersetzen (`encrypted_object`), PATCH auf `is_public`, `original_filename`,
`ai_context_metadata` (`encrypted_one_way`). Es gibt keinen Betreiber-Durchgriff und keinen für den Default-Master.

## Format und Schlüssel

- **Container:** `storage/encryption.py`. AES-256-GCM in 64-KiB-Segmenten, Nonce = 7 Byte Präfix + 4 Byte Zähler + 1 Byte
  Endmarke, AAD = object_key + Mandant + Kopf. Das Chiffrat liegt unter `<upload_root>/media-enc/<tenant>/enc_<hex>.bin`.
- **Spalten:** `encrypted`, `enc_kid`, `enc_wrapped_dek`, `enc_recovery_kid`, `enc_wrapped_dek_recovery`, `enc_filename`,
  `plain_size`. Sie werden beim Start per Migration angelegt und nur hinzugefügt.
- **Protokoll:** Tabelle `storage_decrypt_log`, eine Zeile je Objekt, Prinzipal und Weg pro 5 Minuten.
- **KMS:** Scaleway Key Manager v1alpha1 über `ScalewayKeyWrapper`. Verpackt wird mit `encrypt` und
  `associated_data`. `decrypt` liefert den DEK unter der neuesten Schlüsselversion mit, er wird beim Lesen nebenbei
  gespeichert.
- **Notfall:** X25519 + HKDF + AES-GCM auf Alex' öffentlichen Schlüssel. Ohne ihn verschlüsselt der Dienst nicht.

## Konfiguration (storage-api.service)

```ini
# /etc/systemd/system/storage-api.service.d/50-encryption.conf
[Service]
Environment=STORAGE_ENCRYPTION_BACKEND=scaleway
Environment=STORAGE_ENCRYPTION_SCW_KEY_ID=<key id>
Environment=STORAGE_ENCRYPTION_SCW_REGION=fr-par
Environment=STORAGE_ENCRYPTION_RECOVERY_PUBKEY=<base64 aus keygen>
LoadCredential=storage-kms-token:/etc/credstore/storage-kms-token
# optional, für Mitteilungen an den Besitzer:
# Environment=STORAGE_EVENTS_REDIS_URL=redis://...
```

- `/etc/credstore/storage-kms-token` ist `0400 root:root` und enthält nur den Secret Key der IAM-Anwendung. Er steht nie
  in `.env`, Git, Logs oder einem Chat.
- IAM-Policy: nur Encrypt und Decrypt auf diesen einen Schlüssel. Rotieren und `disable_key` macht Alex bzw. Cloud mit
  einem eigenen Zugang. Auf den Schlüssel `protect_key` setzen.
- Den Notfallschlüssel erzeugt Alex auf seinem eigenen Rechner:
  `python3 scripts/encryption_admin.py keygen --out alex-recovery.key`. Zwei Offline-Kopien, nie auf einem Server.
- Das Test-Backend (`STORAGE_ENCRYPTION_BACKEND=test`) lädt nur mit `STORAGE_ENCRYPTION_ALLOW_TEST_BACKEND=1` und ist
  nur für Tests und Testinstanzen gedacht.

## Betriebsumstellung auf arkserver (vor dem Go-live, angekündigt, Cloud schaut mit)

Grund: `kernel.yama.ptrace_scope=0`. Jeder Prozess mit derselben UID kann den Speicher eines anderen lesen, heute teilt
storage-api `www-data` mit php-fpm, nginx und Nachbar-APIs. Dazu ist Swap aktiv, und `/tmp` liegt auf ext4.

1. **Benutzer und Gruppen:**
   - `storageapi` für storage-api, `storageworker` für die 4 Worker, eine gemeinsame Gruppe `storage-db`.
   - Beide behalten die Zusatzgruppe `www-data` für den bestehenden Upload-Baum (2775, setgid www-data). nginx liefert
     HLS weiter aus.
2. **Rechte:**
   - `/var/lib/storage-api` auf `root:storage-db 2770`.
   - `storage.db`, `-wal` und `-shm` auf `0660 :storage-db`.
   - `media-enc/` auf `0700 storageapi:storageapi`.
3. **Drop-in storage-api:**

   ```ini
   [Service]
   User=storageapi
   Group=storageapi
   SupplementaryGroups=storage-db www-data
   UMask=0002
   LimitCORE=0
   MemorySwapMax=0
   ProtectProc=invisible
   TemporaryFileSystem=/tmp
   ```

4. **Worker:** `User=storageworker`, `SupplementaryGroups=storage-db www-data`, `UMask=0002`, kein KMS-Credential. Das gilt
   für celery-worker, storage-ai-worker, storage-safety-worker und storage-embeddings-worker. Bisher stand das in
   `30-www-data.conf`.
5. **Weitere DB-Schreiber:**
   - Als root laufen celery-beat, storage-opencloud-sync, storage-cache-cleanup und `/etc/cron.d/storage-ttl-cleanup`.
     Sie bekommen `UMask=0002` bzw. `umask 0002`, sonst legt ein root-Lauf nach einem Neustart `-wal`/`-shm` mit 0644
     an.
   - `storage-ttl-cleanup` läuft als root oder als `storageapi`, **nicht** als `storageworker`, weil er Chiffrat in
     `media-enc/` löschen muss.
   - Der doppelte Eintrag `cleanup_expired.py` in der Crontab von `www-data` entfällt.
6. **nginx:** In den vhosts von storage-api **und** cloud-api auf den Upload-Routen `proxy_request_buffering off`
   setzen, oder `client_body_temp_path` auf tmpfs legen.
7. **cloud-api (Cloud-worker):** `TemporaryFileSystem=/tmp`. Bei `encrypted=true` gibt es keinen Rückfall auf
   `UPLOAD_DIR`, `ttl_hours=0`, und der Dienst bricht fail-closed ab, wenn Storage `encrypted: true` nicht bestätigt.

## Go-live-Checkliste

- [ ] Scaleway-Konto, Schlüssel (symmetric_encryption) mit `protect_key`, IAM-Anwendung mit minimaler Policy
- [ ] Credential unter `/etc/credstore/storage-kms-token`
- [ ] Notfallschlüssel bei Alex (zwei Offline-Kopien), öffentlicher Teil im Drop-in
- [ ] Betriebsumstellung oben, danach alle Dienste grün und eine Upload-/HLS-Probe
- [ ] #1960: eigener Gateway-Schlüssel. Vorher sieht jeder mit dem Default-Literal die Metadaten-Zeile verschlüsselter
      Agenten-Uploads.
- [ ] mcp-server (`encrypted` an `assets_upload*`, Name aus Content-Disposition), cloud-api und CloudV2
- [ ] Probe: Upload, Lesen als Eigentümer, signiertes Lesen, 403 für Fremde, Eintrag in `storage_decrypt_log`,
      `export` mit dem Notfallschlüssel

## Notfall und Rotation

```bash
# nach rotate_key bei Scaleway (danach alte Version per disable_key sperren):
sudo -u storageapi ./venv/bin/python scripts/encryption_admin.py rewrap-kms --dry-run
sudo -u storageapi ./venv/bin/python scripts/encryption_admin.py rewrap-kms

# KMS-Konto verloren, neuer KMS ist konfiguriert, privater Notfallschlüssel kurz eingelegt:
./venv/bin/python scripts/encryption_admin.py recover-rewrap --private-key /media/usb/alex-recovery.key

# ein Objekt ohne KMS herausholen:
./venv/bin/python scripts/encryption_admin.py export 134999 --private-key ... --out /root/134999.bin
```

**Löschen:** Die Zeile mit dem verpackten DEK und das Chiffrat werden gelöscht. Endgültig weg ist das Objekt erst,
wenn alle DB-Sicherungen mit dieser Zeile abgelaufen sind. Eine KMS-Rotation reicht dafür nicht.

**Rückfall:** Eine Version ohne dieses Feature findet in `media/` nichts und antwortet 404. Sie liefert nie Chiffrat
aus.
