# OpenCloud-Ordner als Referenz-Objekte (#2810)

Konzept: Content-Post 5939, Produkt „Konzept: OpenCloud-Ordner als Quelle für Beiträge und Storage-Objekte“, freigegeben am 13.11.2026.

## Was es tut

- `python -m storage.opencloud --bindings <datei>` gleicht jeden freigeschalteten Ordner mit seiner Collection ab. Mit `--dry-run` zeigt es nur, was es tun würde.
- Neue Dateien werden zu `storage_mode=reference`-Objekten mit `opencloud_space_id` und `opencloud_file_id`. Die Datei-ID kommt aus dem xattr `user.oc.id` des Posix-Treibers.
- Ändert sich eine Datei (mtime oder Größe), werden Prüfsumme und Größe neu berechnet und die Vorschauen verworfen. Damit ändert sich auch das ETag.
- Eine verschobene oder umbenannte Datei wird über ihre Datei-ID im selben Space wiedergefunden.
- Fehlt eine Datei, bekommt das Objekt `reference_state=missing`, und `/storage/media/{id}` antwortet mit 404 `file_missing`. Gelöscht wird nichts. Kommt die Datei zurück, hängt der nächste Abgleich das Objekt wieder an.

## Pflichtregel 8: Storage liest nur

- `www-data` bekommt per ACL nur `r-x` auf die freigeschalteten Ordner, nie `w`.
- Ein gelöschtes Storage-Objekt lässt die Datei stehen (`_perform_delete` bei `storage_mode=reference`).
- Ein Binding muss innerhalb eines Space unter `OPENCLOUD_PROJECTS_ROOT` liegen. Die privaten Laufwerke unter `users/` sind ausgeschlossen.
- Bei `sensitive: true` gilt immer `ai_mode=none`: keine Analyse und keine Embeddings.

## Konfiguration

| Variable | Vorgabe |
|---|---|
| `OPENCLOUD_PROJECTS_ROOT` | `/var/lib/opencloud/storage/users/projects` |
| `OPENCLOUD_BINDINGS_PATH` | leer, also kein Abgleich |

Format der Bindings: siehe `opencloud_bindings.example.json`. Die Liste pflegt FileSync.

Voraussetzung fürs Ausrollen ist PR #25 (#2680).
