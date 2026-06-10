# Image prompt conventions — The Mythscape (Flux.1-schnell via mflux)

The script LLM emits 5–7 image prompts per short — `shorts-api` passes each
to `mflux generate` and renders at 768×1344 (9:16) in 4 steps. ~15–25s per
image on Apple Silicon, ~90–150s total for a 6-image short.

The painterly anchors below are **shared across every channel** in this
pipeline. Don't re-skin them per topic — visual consistency is what builds
a recognizable house style across mythology, etymology, and any future
sister channel.

## Why these constraints

- **Painterly, not photoreal.** Photoreal AI images of gods and heroes read
  as "AI slop" instantly and trip YouTube's inauthentic-content filter.
  Oil-on-canvas painterly images read as "stylized art direction" — closer
  to a museum wall card than a stock photo — and survive review.
- **No text in images.** Flux can't spell, and divine names in non-Latin
  scripts (Devanagari, Hieroglyphs, Cuneiform) come out as garbled noise.
  We burn typography on top with `drawtext` in ffmpeg — full control, perfect
  kerning, correct transliteration.
- **No frontal faces of gods.** Faces drift between images, and a face that
  isn't consistent across the 6-image sequence breaks the "this is one
  story about one character" illusion. Backs, three-quarter profiles,
  silhouettes, hands, sacred objects, and from-behind compositions hold
  the character anchor without the uncanny-face problem.
- **9:16 native.** Don't render square and crop — wastes pixels and
  recomposes the focal point badly.

## Style anchor

Every prompt must start with this exact phrase:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light,

This is the channel's visual signature, shared with sister channels. Consistency
across thousands of shorts is what builds the look of a "real" channel vs.
random uploads.

## Closing anchor

Every prompt must end with:

> cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

## Negative scenes to avoid

- Modern interiors / phones / computers / cars (the mythological world only)
- Photographic studio lighting
- Marvel / DC / Disney / Riordan-styled versions of any mythological figure
- Recognizable celebrities or copyrighted characters
- Multiple text-heavy elements (signs, scrolls with readable text, hieroglyph
  walls in focus — fine as soft background bokeh, never as the focal subject)
- "Mood" prompts with no figure or sacred object as the focal subject

## Good examples (for the LLM to imitate)

`fenrir's binding` story beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, the great wolf Fenrir in Viking Age Iceland bound on a barren rocky isle by the deceptively thin silver ribbon Gleipnir, his jaws forced open around the upright sword Týr placed there, the gods watching from a distance, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`inanna's descent` story beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, the goddess Inanna in early Sumerian Uruk stepping through the first of seven gates of the underworld Kur, her tall crown of heaven being lifted from her head by a faceless gatekeeper, ziggurat steps descending into darkness behind her, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`coyote stealing fire` setup beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, Coyote crouched on a high mesa under a cold pre-dawn Pacific Northwest sky, watching a distant glow on a far mountaintop where the Above World fire-keepers tend their flame, his ears pricked forward in calculation, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`set vs horus` story beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, the falcon-headed god Horus and the strange-headed god Set wrestling on a stone barge on the Nile during the Old Kingdom Egyptian flood season, the assembled ennead of gods watching from the riverbank, reed boats and date palms in the background, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`churning of the ocean of milk` setup beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, the great serpent Vasuki coiled around Mount Mandara at the center of the cosmic ocean of milk in Vedic-era myth, devas pulling one end and asuras the other, the mountain spinning slowly to churn the waters, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks
