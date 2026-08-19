# storage-api für Tenant-Instanzen

Was eine isolierte Kundeninstanz braucht, wie sie aktualisiert wird und woran
man prüft, dass sie wirklich isoliert ist.

## Freigabestand

Der Tag `tenant-release` markiert den Stand, der für Kundeninstanzen freigegeben
ist. Er bewegt sich nur, wenn ein Stand bewusst freigegeben wurde — nicht
automatisch mit `main`.

```bash
git fetch --tags && git checkout tenant-release
```

## Pflicht-Umgebung

Ohne diese Werte startet der Dienst zwar, läuft aber falsch. Sie sind alle
`Optional` im Code, es gibt **keine** harte Prüfung beim Start.

| Variable | Warum sie Pflicht ist |
|---|---|
| `API_KEY` | **Sicherheitskritisch.** Ohne sie fällt `config.py` auf einen Wert zurück, der in unserer eigenen Dokumentation steht und als kompromittiert gilt. Pro Installation frisch erzeugen: `openssl rand -hex 32`. Der Dienst schreibt beim Start ein `CRITICAL` ins Log, wenn er auf dem Vorgabewert läuft — dieses Log ist beim ersten Start zu lesen. |
| `DATABASE_URL` | eigene SQLite-Datei unter dem Installationsverzeichnis |
| `CHROMA_DB_PATH` | **Genau dieser Name.** `CHROMA_PERSIST_DIR` wird nirgends gelesen; wer ihn setzt, bekommt lautlos den eingebauten Vorgabepfad. |
| `STORAGE_UPLOAD_DIR` | absolut angeben, sonst relativ zum Arbeitsverzeichnis |
| `TENANT_ID` | eigener Mandantenname, **nicht** `arkturian` |
| `ONEAL_API_KEY` | ausdrücklich **leeren** — der Vorgabewert gehört einem fremden Mandanten |
| `IACP_REDIS_URL` | **nicht setzen.** Ungesetzt bedeutet keine Bus-Anbindung, und genau das ist für eine isolierte Instanz richtig. |

Externe Schlüssel (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) sind **optional**: Der
Dienst startet ohne sie, Upload, Auslieferung und Varianten funktionieren
vollständig. Nur die KI-Analyse fällt aus — und die kostet pro Aufruf Geld, ist
deshalb ohnehin standardmäßig aus (`ai_mode="none"`).
`GEMINI_API_KEY` **nicht** setzen (bei uns wegen eines Kostenvorfalls stillgelegt).

## Erstinstallation

`./setup-new-server.sh <kunden-name> <domain>` richtet Systempakete, venv,
`.env`, Datenbank, systemd-Unit, nginx und Zertifikat ein. Migrationen gibt es
nicht; das Schema entsteht über `Base.metadata.create_all`.

## Aktualisieren

```bash
git fetch --tags && git checkout tenant-release
venv/bin/pip install -r requirements.txt
systemctl restart storage-api storage-embeddings-worker storage-ai-worker
```

Nicht während eines laufenden Stapellaufs aktualisieren: Der venv-Neubau
entfernt kurzzeitig `certifi`, wodurch laufende Aufgaben an der TLS-Prüfung
scheitern.

## Verify-Checkliste

Die ersten beiden Zeilen sind Formsache. **Die dritte ist die wichtige** — sie
ist die einzige, die Isolation tatsächlich beweist.

```bash
# 1. Dienst antwortet
curl -s -o /dev/null -w '%{http_code}\n' https://<domain>/health         # 200

# 2. läuft NICHT auf dem öffentlichen Vorgabeschlüssel
journalctl -u storage-api --since -5min | grep -c 'API_KEY is unset'      # 0

# 3. ein Upload landet in DIESER Instanz, nicht beim Betreiber
curl -s -X POST https://<domain>/storage/upload \
     -H "X-API-KEY: $API_KEY" -F 'file=@probe.txt' | jq -r .id            # -> ID merken
sqlite3 <install>/storage.db 'SELECT id, tenant_id FROM storage_objects ORDER BY id DESC LIMIT 1'
#    Die ID MUSS hier auftauchen und den eigenen tenant_id tragen.
curl -s -o /dev/null -w '%{http_code}\n' \
     https://api-storage.arkturian.com/storage/media/<ID>                 # 404 erwartet
#    Ein 200 hieße: das Material liegt beim Betreiber. Dann sofort stoppen.
```

Prüfung 3 ist aus einem realen Vorfall entstanden: Ein ungesetzter Client-Schlüssel
ließ Kundenmaterial in der Betreiber-Instanz landen, ohne dass irgendwo ein
Fehler auftrat. Beide Instanzen antworteten normal — nur eben die falsche.

## Was in einer Tenant-Instanz NICHT laufen soll

- **Kein `IACP_REDIS_URL`** — sonst hängt die Instanz am Föderations-Bus.
- **Keine Übernahme von Betreiber-Daten.** Das Setup legt eine leere Datenbank
  an; es gibt keinen Import-Schritt, und es soll auch keinen geben.
- **Kein geteilter Chroma-Pfad.** Zwei Dienste mit unterschiedlichem
  `CHROMA_DB_PATH` schreiben in getrennte Vektordatenbanken, ohne dass es
  auffällt — bei uns lief das monatelang unbemerkt.
