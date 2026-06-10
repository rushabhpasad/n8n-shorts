# Image prompt conventions (Flux.1-schnell via mflux)

The script LLM emits 3 image prompts per short — `shorts-api` passes each to
`mflux generate` and renders at 768×1344 (9:16) in 4 steps. ~15–25s per image
on M1 Max, ~60s total for 3 images.

## Why these constraints

- **Painterly, not photoreal.** Photoreal AI images read as "AI slop" instantly
  and trip YouTube's inauthentic-content filter. Painterly oil-style images
  read as "stylized art direction" and survive.
- **No text in images.** Flux can't spell. We burn typography on top with
  `drawtext` in ffmpeg — full control, perfect kerning.
- **No frontal humans.** Faces drift between images and look uncanny across
  the 3-beat sequence. Backs, silhouettes, hands, objects are safer.
- **9:16 native.** Don't render square and crop — wastes pixels and recomposes
  the focal point badly.

## Style anchor

Every prompt must start with this exact phrase:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light,

This is the channel's visual signature. Consistency across thousands of shorts
is what builds the look of a "real" channel vs. random uploads.

## Closing anchor

Every prompt must end with:

> cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

## Negative scenes to avoid

- Modern interiors / phones / computers (unless the word is genuinely modern)
- Photographic studio lighting
- Recognizable celebrities or copyrighted characters
- Multiple text-heavy elements (signs, books with readable pages)

## Good examples (for the LLM to imitate)

`nostalgia` beat 2 (origin):

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, a lone 17th century Swiss mercenary soldier viewed from behind, sitting on a cold alpine ridge at dusk, distant snow-capped peaks, a single melting candle on a rock beside him, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`salary` beat 2 (origin):

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, a Roman legionary's calloused hand pouring coarse white salt crystals into a small leather pouch on a wooden table, oil lamp glow, ancient Roman barracks in background bokeh, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`assassin` beat 2 (origin):

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, a hooded figure in dark robes walking away down a moonlit medieval Persian alley, an ornate dagger half-hidden under the cloak, lantern light spilling from a doorway, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks
