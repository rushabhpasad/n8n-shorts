# Image prompt conventions — Open Verdicts (Flux.1-schnell via mflux)

The script LLM emits 5–7 image prompts per short — `shorts-api` passes each
to `mflux generate` and renders at 768×1344 (9:16) in 4 steps. ~15–25s per
image on Apple Silicon, ~90–120s total per short.

## Why these constraints

- **Painterly, not photoreal.** Photoreal AI images of historical events
  read as "AI slop" instantly and trip YouTube's inauthentic-content
  filter. Painterly oil-style images read as stylized art direction and
  survive. Doubly important on this channel, where photoreal renderings of
  real deceased people would be tasteless and legally fraught.
- **No text in images.** Flux can't spell, and any rendered "newspaper" or
  "report" will be gibberish. We burn typography on top with `drawtext` in
  ffmpeg — full control, perfect kerning.
- **No realistic faces of historical victims.** This is non-negotiable on
  Open Verdicts. Real deceased people (Anna Anderson, the Sodder children,
  the Hinterkaifeck family, Glenn Miller, etc.) must not appear with
  legible facial features. Use backs of heads, hands, silhouettes,
  shadow-on-wall, period clothing without a face, or the absence of a
  person (an empty chair, a vacant cot, a coat on a hook).
- **No gore.** The channel's register is *implied absence*, not graphic
  injury. An empty lifeboat says more than a body.
- **9:16 native.** Don't render square and crop — wastes pixels and
  recomposes the focal point badly.

## Style anchor

Every prompt must start with this exact phrase:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light,

This is the channel's visual signature. Consistency across thousands of
shorts is what builds the look of a real channel vs. random uploads.

## Closing anchor

Every prompt must end with:

> cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

## Faces and likeness — STRICT

- **No realistic frontal faces of real historical victims, investigators,
  witnesses, or named individuals.** Substitute:
  - back-of-head shots
  - hands (a coroner's gloved hand, a sailor's calloused hand on a rope)
  - silhouettes against a window or lantern
  - shadows cast on a wall
  - objects belonging to the person (a coat draped over a chair, a
    monogrammed pocket watch on a table)
  - scenes viewed from behind, above, or through a doorway
- Period-appropriate bystanders in long shot, with faces obscured by
  hats, hoods, distance, or fog, are acceptable.

## Negative scenes to avoid

- Modern interiors, phones, computers, post-1960 visual cues (unless the
  case itself is borderline-1960s and the artifact is the focal subject)
- Photographic studio lighting
- Recognizable celebrities or copyrighted characters
- Realistic frontal portrait renderings of named historical people
- Gore, visible wounds, blood, or graphic injury
- Multiple text-heavy elements (signs, books with readable pages,
  newspaper headlines we can read)

## Atmospheric subject examples (the channel's register)

- A foggy harbor at dawn, a ship's lantern still lit
- A candlelit ship's log open mid-sentence
- Footprints fading into deep snow on a forested slope
- An abandoned campsite with a slashed canvas tent
- A torn page of a 15th century manuscript covered in unknown script
- A coroner's gloved hand resting on a death certificate stamped "open"
- An empty lifeboat tied to a brig's stern
- A wooden coroner's bench with case files stacked, gas lamp burning low
- A torn cipher slip half-pulled from a tailored coat pocket
- A vacant chair beside a fireplace, a half-finished letter on the desk
- A row of bronze gears on a workshop bench, lit by oil lamp

## Good examples (for the LLM to imitate)

`mary celeste` evidence beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, the empty wooden deck of an American merchant brig in November 1872, coiled rope, a swinging brass lantern, a half-open hatch, no human figures, late afternoon sun raking across damp planks, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`voynich manuscript` evidence beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, an open 15th century vellum manuscript page on a wooden reading desk in a candlelit Italian scriptorium, covered in an unknown looping script and an illustration of an impossible plant with star-shaped leaves, viewed from above, no human figures, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`dyatlov pass` evidence beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, an abandoned canvas tent half-buried in deep snow on the eastern slope of a 1959 Ural foothill at twilight, the tent wall slashed open from the inside, no human figures, footprints leading downhill into the trees, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks
