# Storage API - Media Endpoint

## Overview

The media endpoint serves optimized media variants with on-demand transformation, caching, and video frame extraction.

**Base URL:** `https://api-storage.arkturian.com/storage/media/{id}`

## Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `variant` | string | - | `thumbnail` (320px), `medium` (1920px), `full` (original) |
| `width` | int | - | Custom width in pixels |
| `height` | int | - | Custom height in pixels |
| `format` | string | original | Images/video-frame: `jpg`, `png`, `webp`. Audio (or a video's audio track): `mp3`, `m4a`, `aac`, `ogg`, `opus`, `wav`, `flac` |
| `quality` | int | 85 | 1-100 (JPEG/WebP compression quality) |
| `bitrate` | string | codec default | Audio only: target bitrate for lossy formats, e.g. `192k` or `192000` (ignored for `wav`/`flac`) |
| `sample_rate` | int | source | Audio only: output sample rate in Hz, e.g. `44100` |
| `channels` | int | source | Audio only: output channels (`1`=mono, `2`=stereo) |
| `aspect_ratio` | string | - | e.g., `1:1`, `16:9` (letterboxing without stretch) |
| `trim` | bool | false | Crop using stored trim bounds |
| `refresh` | bool | false | Clear cache and regenerate |
| `download` | bool | false | Serve `Content-Disposition: attachment` (browser "Save As" with the real filename) instead of inline render/play. Keyless for public objects. |

## Variants

### thumbnail
- Default size: 320px (longest edge)
- Cached after first generation
- Best for: list views, map markers, previews

### medium
- Default size: 1920px (longest edge)
- Cached after first generation
- Best for: web display, detail views

### full
- Original file, no transformation
- Streams directly from storage
- Best for: downloads, high-quality display

## Examples

### Basic Usage

```typescript
// Thumbnail (320px default)
const thumb = `/storage/media/${id}?variant=thumbnail`

// Medium for web display
const medium = `/storage/media/${id}?variant=medium`

// Original file
const original = `/storage/media/${id}?variant=full`
```

### Custom Dimensions

```typescript
// Fixed width, auto height
const small = `/storage/media/${id}?width=100`

// Fixed height, auto width
const tall = `/storage/media/${id}?height=400`

// Both dimensions (may crop)
const exact = `/storage/media/${id}?width=400&height=300`
```

### Format Conversion

```typescript
// Convert to WebP (smaller file size)
const webp = `/storage/media/${id}?format=webp&quality=80`

// Convert to JPEG
const jpeg = `/storage/media/${id}?format=jpg&quality=85`

// Convert to PNG (lossless)
const png = `/storage/media/${id}?format=png`
```

### Aspect Ratio (Letterboxing)

```typescript
// Square with letterboxing
const square = `/storage/media/${id}?width=200&aspect_ratio=1:1`

// 16:9 for video thumbnails
const widescreen = `/storage/media/${id}?width=400&aspect_ratio=16:9`
```

## Video Handling

### Important: Video Frame Extraction

When the source is a video file (MP4, MOV, etc.), you **must** specify `format=jpg` (or `png`/`webp`) to extract a frame. Without a format parameter, the endpoint returns the full video file.

```typescript
// WRONG - Returns full video (could be 72MB+)
const bad = `/storage/media/${videoId}?variant=thumbnail`

// CORRECT - Extracts frame as JPEG (~3-30KB)
const good = `/storage/media/${videoId}?variant=thumbnail&format=jpg`
```

### Video Frame Extraction Details

- Frame is extracted from the **middle** of the video (50% duration)
- Uses ffmpeg for extraction
- Supports all common video formats (MP4, MOV, AVI, MKV, WebM)
- Thumbnail is cached after first generation

### Complete Video Examples

```typescript
// Small thumbnail for map markers (100px JPEG)
const mapThumb = `/storage/media/${id}?width=100&format=jpg`

// Medium preview for gallery
const preview = `/storage/media/${id}?variant=medium&format=jpg`

// WebP for modern browsers
const webpThumb = `/storage/media/${id}?variant=thumbnail&format=webp&quality=80`
```

## Keyless Download / Share Links

`GET /storage/media/{id}` is the canonical **keyless public read** path for any
object with `is_public=true` (200, no `X-API-KEY`). There is no separate signed /
expiring / VOD share endpoint — these links are permanent while the object stays
public. (`GET /storage/files/{id}` is key-gated; do not use it for public links.)

By default the file is served **inline** (the browser plays/renders it). Append
`?download=1` to force a clean **"Save As" with the original filename**:

```typescript
// Inline (browser plays the mp3 in its audio player)
const play = `https://api-storage.arkserver.arkturian.com/storage/media/${id}`

// Download as "Artist - Song.mp3" (Content-Disposition: attachment, keyless)
const dl = `https://api-storage.arkserver.arkturian.com/storage/media/${id}?download=1`
```

`download=1` emits both a sanitized `filename=` and an RFC 5987 `filename*=UTF-8''`
so unicode names survive. It composes with `format` (e.g. `?format=mp3&download=1`
downloads a converted derivative). HEAD mirrors the attachment disposition.

## Audio Handling

### Audio Codec Conversion

The endpoint transcodes audio on the fly — the audio analogue of image `?format=jpg`.
Request any target container via `?format=`:

```typescript
// Opus voice note → MP3 (universally playable)
const mp3 = `/storage/media/${id}?format=mp3`

// → WAV (uncompressed PCM), FLAC (lossless), M4A/AAC, OGG/Vorbis, Opus
const wav = `/storage/media/${id}?format=wav`

// Tune the lossy output: bitrate, sample rate, channels
const small = `/storage/media/${id}?format=m4a&bitrate=96k&sample_rate=22050&channels=1`
```

Supported target formats: `mp3`, `m4a`, `aac`, `ogg`, `opus`, `wav`, `flac`.

### Details

- ffmpeg-backed, bounded by the same concurrency semaphore as video frame extraction.
- Derivatives are **cache-keyed** by `(id, format, bitrate, sample_rate, channels)`, so
  repeat requests never re-run ffmpeg.
- Works on **video sources too**: `?format=mp3` on a video demuxes and encodes its
  audio track (`-vn`). `?format=jpg` still extracts a frame — the two never collide.
- **Without** an audio `format` param, audio is served **raw** (backwards compatible).
- `bitrate`/`sample_rate`/`channels` are ignored for lossless `wav`/`flac`.
- Requesting an audio `format` on a non-audio/non-video object (image, PDF, GLB)
  returns **415** rather than a silent wrong response.
- If the source already **is** the requested container and no re-encode knobs are
  supplied, the original is served unchanged (no needless transcode pass).

```typescript
// WRONG - image cannot become audio → 415 Unsupported Media Type
const bad = `/storage/media/${imageId}?format=mp3`

// CORRECT - audio (or a video's audio track) → MP3
const good = `/storage/media/${audioId}?format=mp3`
```

## TypeScript/Frontend Usage

### Building URLs

```typescript
function buildMediaUrl(
  id: number,
  options: {
    variant?: 'thumbnail' | 'medium' | 'full'
    width?: number
    height?: number
    format?: 'jpg' | 'png' | 'webp'
    quality?: number
    aspectRatio?: string
  } = {}
): string {
  const base = `https://api-storage.arkturian.com/storage/media/${id}`
  const params = new URLSearchParams()

  if (options.variant) params.set('variant', options.variant)
  if (options.width) params.set('width', String(options.width))
  if (options.height) params.set('height', String(options.height))
  if (options.format) params.set('format', options.format)
  if (options.quality) params.set('quality', String(options.quality))
  if (options.aspectRatio) params.set('aspect_ratio', options.aspectRatio)

  const qs = params.toString()
  return qs ? `${base}?${qs}` : base
}

// Usage
const thumb = buildMediaUrl(123, { variant: 'thumbnail', format: 'jpg' })
const small = buildMediaUrl(123, { width: 100, format: 'jpg' })
```

### React Component Example

```tsx
interface StorageImageProps {
  id: number
  variant?: 'thumbnail' | 'medium' | 'full'
  width?: number
  format?: 'jpg' | 'webp'
  alt: string
  className?: string
}

function StorageImage({
  id,
  variant = 'thumbnail',
  width,
  format = 'jpg',
  alt,
  className
}: StorageImageProps) {
  const params = new URLSearchParams()
  params.set('variant', variant)
  params.set('format', format)
  if (width) params.set('width', String(width))

  const src = `https://api-storage.arkturian.com/storage/media/${id}?${params}`

  return <img src={src} alt={alt} className={className} loading="lazy" />
}

// Usage
<StorageImage id={123} variant="thumbnail" alt="Product" />
<StorageImage id={456} width={100} format="webp" alt="Marker" />
```

### Handling Mixed Media (Images + Videos)

```typescript
function getOptimizedThumbUrl(
  storageId: number,
  mimeType?: string,
  width: number = 100
): string {
  const base = `https://api-storage.arkturian.com/storage/media/${storageId}`
  const params = new URLSearchParams()

  params.set('variant', 'thumbnail')
  params.set('width', String(width))

  // Always request JPG format for videos (frame extraction)
  // Also good for images (universal format)
  params.set('format', 'jpg')

  return `${base}?${params}`
}
```

## Caching

- All transformed variants are cached on the server
- Cache key: `{id}_{variant}_{width}_{height}_{format}_{quality}_{aspect_ratio}`
- Use `?refresh=true` to force regeneration
- Browser caching respects standard HTTP cache headers

## Error Handling

| Status | Description |
|--------|-------------|
| 200 | Success - returns media |
| 404 | Object not found |
| 422 | Invalid parameters |
| 500 | Processing error (check logs) |

## Performance Tips

1. **Use thumbnails for lists** - Don't load full images for grid/list views
2. **Specify width for markers** - Request exact size needed (e.g., `width=100`)
3. **Use WebP when possible** - ~30% smaller than JPEG
4. **Always add format for videos** - Prevents accidental full video download
5. **Cache URLs in frontend** - Don't rebuild URLs on every render

## MCP Server Integration

The Storage MCP server provides tools for media access:

```typescript
// Get preview URL
mcp__storage__media_preview({ id: 123, variant: 'thumbnail', format: 'jpg' })

// Get as base64 data URL (for CSP-restricted environments)
mcp__storage__media_as_data_url({ id: 123, width: 100, format: 'jpg' })
```
