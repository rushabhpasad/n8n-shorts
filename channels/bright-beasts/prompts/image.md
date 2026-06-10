# Image prompt conventions — Bright Beasts (Flux.1-schnell via mflux)

The script LLM emits 5–7 image prompts per short — `shorts-api` passes each to
`mflux generate` and renders at 768×1344 (9:16) in 4 steps. ~15–25s per image
on Apple Silicon, ~90–150s total per short.

## Why these constraints

- **Painterly, not photoreal.** Photoreal AI images of animals read as
  "AI stock photo" instantly and trip YouTube's inauthentic-content filter.
  Painterly oil-style images read as "stylized art direction" and survive.
  It also keeps the channel visually distinct from the wildlife documentary
  shelf the algorithm will compare it against.
- **Behavior, not portrait.** A cute parrot head-shot tells you nothing
  about Alex learning "none". The image must show the animal *performing
  the specific behavior the study documented* — bending the wire, touching
  the mirror mark, unscrewing the jar lid, caching the worm.
- **No text in images.** Flux can't spell. We burn typography on top with
  `drawtext` in ffmpeg — full control, perfect kerning.
- **No researcher faces.** Faces drift between images and look uncanny.
  Hands, silhouettes, backs of heads, and the equipment itself are safer.
  The animal is the focal subject; the human is context, not character.
- **9:16 native.** Don't render square and crop — wastes pixels and
  recomposes the focal point badly.

## Style anchor

Every prompt must start with this exact phrase:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light,

This is the channel's visual signature — shared with the other channels in
the same brand family. Consistency across thousands of shorts is what builds
the look of a "real" channel vs. random uploads.

## Closing anchor

Every prompt must end with:

> cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

## The behavior rule (channel-specific)

**The animal must be doing the specific behavior — not a portrait.**

- Show the wire bent into a hook in the crow's beak, mid-extraction from
  the vertical tube.
- Show the elephant's trunk tip raised to touch the white X painted above
  its eye in the mirror.
- Show the magpie pulling the yellow sticker off its own chest feathers in
  front of the mirror.
- Show the octopus draped sideways across the jar with two arms unscrewing
  the lid.
- Show the scrub jay caching the wax-worm under a specific pebble in the
  tray, glancing sideways at a conspecific.
- Show the chimp Mama in her straw nest reaching her hand up to cup Jan
  van Hooff's face — not "a chimp portrait".

If the prompt could equally describe a generic wildlife photo of the
species, it is failing this rule — rewrite it to include the named object
and the named action.

## Negative scenes to avoid

- Cute "portrait" shots of the species with no specific behavior
- Cartoonish anthropomorphism (animals in clothes, animals at desks)
- Photographic studio lighting / wildlife-documentary photo realism
- Modern interiors that contradict the actual research setting
- Recognizable researchers' faces (use hands, silhouettes, equipment instead)
- Multiple text-heavy elements (signs, books with readable pages)
- More than one focal subject per prompt — split into two prompts instead

## Good examples (for the LLM to imitate)

`betty the new caledonian crow` finding beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, a New Caledonian crow gripping a length of garden wire in its beak inside the 2002 Oxford behavioural ecology lab, the wire freshly bent into a hooked shape against the rim of a clear vertical tube, a small bucket of food visible at the bottom of the tube, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`happy the elephant` finding beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, an Asian elephant standing in the 2006 Bronx Zoo elephant yard in front of a tall wall-mounted mirror, her trunk tip lifted to touch a chalk-white X painted on her forehead above the left eye, the same X visible in her mirror reflection, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`inky the octopus` finding beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, a common octopus draped over the edge of a tank in the 2016 National Aquarium of New Zealand at night, two arms reaching down toward a floor drain, the rest of the body flowing across the lip of the tank, dim safety lighting, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks

`mama the chimp` implication beat:

> painterly illustration, oil-on-canvas texture, muted earth tones, soft natural light, an elderly female chimpanzee curled in a straw nest at the 2016 Royal Burgers' Zoo Arnhem, her hand cupping the back of a researcher's silhouetted head (face turned away from camera), the gesture gentle and unmistakable, late afternoon light, cinematic composition, 9:16 vertical, atmospheric, no text, no captions, no watermarks
