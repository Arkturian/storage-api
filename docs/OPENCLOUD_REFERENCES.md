# OpenCloud-Ordner als Referenz-Objekte (#2810)

Konzept: Content-Post 5939, Produkt „Konzept: OpenCloud-Ordner als Quelle für Beiträge und Storage-Objekte“, freigegeben am 13.11.2026.

## Was es tut

- `python -m storage.opencloud --bindings <datei>` gleicht jeden freigeschalteten Ordner mit seiner Collection ab. Mit `--dry-run` zeigt es nur, was es tun würde.
- Neue Dateien werden zu `storage_mode=reference`-Objekten mit `opencloud_space_id` und `opencloud_file_id`. Die Datei-ID kommt aus dem xattr `user.oc.id` des Posix-Treibers.
- Ändert sich eine Datei (mtime oder Größe), werden Prüfsumme und Größe neu berechnet und die Vorschauen verworfen. Damit ändert sich auch das ETag.
- Eine verschobene oder umbenannte Datei wird über ihre Datei-ID im selben Space wiedergefunden.
- Fehlt eine Datei, antwortet `/storage/media/{id}` mit 404 `file_missing`. Ist sie nicht lesbar (EACCES), antwortet es mit 503 `file_unreadable`. Den Zustand ändert die Route dabei nicht.
- Als fehlend (`reference_state=missing`) markiert nur der Ordner-Abgleich, und nur, wenn er den ganzen Ordner lesen konnte. Ein Ordner ohne Leserecht ist ein Fehler dieses Laufs; in dem Lauf wird nichts als fehlend markiert. Gelöscht wird nie etwas. Kommt die Datei zurück, hängt der nächste Abgleich das Objekt wieder an.

## Pflichtregel 8: Storage liest nur

- `www-data` bekommt per ACL nur `r-x` auf die freigeschalteten Ordner, nie `w`.
- Ein gelöschtes Storage-Objekt lässt die Datei stehen (`_perform_delete` bei `storage_mode=reference`).
- Ein Binding liegt entweder innerhalb eines Space unter `OPENCLOUD_PROJECTS_ROOT` oder in einer schreibgeschützten bindfs-Sicht unter `OPENCLOUD_VIEWS_ROOT` (Vorgabe `/srv/opencloud-views`). Für eine Sicht ist das Feld `space_id` Pflicht. Die privaten Laufwerke unter `users/` sind ausgeschlossen.
- Einen Binding-Pfad, den der laufende Benutzer beschreiben kann, lehnt der Abgleich ab.
- Bei `sensitive: true` gilt immer `ai_mode=none`: keine Analyse und keine Embeddings.

## Konfiguration

| Variable | Vorgabe |
|---|---|
| `OPENCLOUD_PROJECTS_ROOT` | `/var/lib/opencloud/storage/users/projects` |
| `OPENCLOUD_BINDINGS_PATH` | leer, also kein Abgleich |
| `OPENCLOUD_VIEWS_ROOT` | `/srv/opencloud-views` |

Format der Bindings: siehe `opencloud_bindings.example.json`. Die Liste pflegt FileSync.

Voraussetzung fürs Ausrollen ist PR #25 (#2680).
