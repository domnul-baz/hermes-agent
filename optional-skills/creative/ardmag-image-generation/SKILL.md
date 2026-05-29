---
name: ardmag-image-generation
description: Produce hero / social images for ARDmag blog articles by detecting the real products mentioned in the article, mapping them to real product assets under backend/static/images/<handle>/, attaching those real packshots to Codex/imagegen as image references, and assembling an organic workshop-scene prompt. Refuses to reuse generic category assets when specific products are named, refuses to generate without real packshots attached (which makes the model invent packaging), refuses collage/cutout/floating-product framing, and emits a cache-busting semantic filename plus a production-validation checklist.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [creative, image-generation, ardmag, blog, hero, social]
    related_skills: [visual-asset-review, baoyu-article-illustrator]
    category: creative
    homepage: https://ardmag.ro
---

# ARDmag Image Generation

Generate hero and social images for ARDmag blog articles that look like editorial photography from a stone workshop, with the actual products from the article integrated organically into the scene.

## When to Use

- A new ARDmag blog article needs a hero image
- An existing hero is conceptually wrong (generic, missing product labels, doesn't match the article's products)
- Social / OG previews are needed for an existing article
- The production site still shows an old hero after a hero swap (cache-busting may be required)

This skill is for the `ardmag.com/site` content repo. Articles live at `backend-storefront/content/blog/*.md`. Product assets live at `backend/static/images/<handle>/`.

## Required Inputs

- Path to the article `.md` (frontmatter + body)
- Asset root: `<site-root>/backend/static/images/`
- Optional: catalog CSV at `<site-root>/docs/catalog_products-*.csv` or `<site-root>/resources/Wix Products Catalog.csv`

## Procedure

### 1. Build the image brief

Find the skill's script dir:

```bash
SKILL_DIR=$(dirname "$(find ~/.hermes/skills -path '*/ardmag-image-generation/SKILL.md' 2>/dev/null | head -1)")
# If the skill is not yet installed into the hub, fall back to the repo copy:
SKILL_DIR=${SKILL_DIR:-/home/dc/.hermes/hermes-agent/optional-skills/creative/ardmag-image-generation}
```

Run the brief builder:

```bash
python "$SKILL_DIR/scripts/ardmag_brief.py" build-brief \
  --article path/to/article.md \
  --assets-root path/to/backend/static/images \
  --catalog path/to/catalog.csv     # optional
```

The brief is JSON with:

- `brand` — inferred brand (`delta-research`, `tenax`, `mixed`, or `none`)
- `products` — list of `{name, slug, asset_dir, sample_image, reference_images, in_catalog}` for every product mentioned in the article
- `reference_images` — flat, de-duplicated list of the real packshot file paths across all specific products. **These must be attached to the image generator as references** (hero, OG, and every social-wave post). The prompt names the products; this list is what makes the model render the real packaging instead of inventing it.
- `prompt` — the assembled image-gen prompt, ready to pass to `image_generate`
- `suggested_filename` — semantic, cache-busting filename for the new hero
- `frontmatter_patch` — the new `heroImage:` value to write to the article frontmatter
- `validation_checklist` — steps to verify in production after deploy

Check `brief.blockers` first. If non-empty, STOP and resolve the cause:

- `products` empty or only generic category matches (`solutii-delta`, `tratamente-specifice`) — generic-only matches are the failure mode this skill exists to prevent.
- Specific products detected but `reference_images` is empty — there are no real packshots to attach. Locate/download the real product images into `backend/static/images/<handle>/` (or pass them explicitly) before generating. Generating from names alone is what made the 2026-05-26 social wave invent packaging.

### 2. Generate the image (attach the real packshots)

Pass `brief.prompt` **and attach every path in `brief.reference_images` as image references** to the generator (Codex / `image_generate`). The prompt names the products and forbids collage/cutout framing; the attached packshots are what force the real packaging. Never generate from the prompt alone when `reference_images` is non-empty — that reintroduces the invented-packaging bug.

```python
result = image_generate(
    prompt=brief["prompt"],
    reference_images=brief["reference_images"],  # real ARDmag packshots
    aspect_ratio="landscape",
)
```

If the generator takes a single composite reference, assemble a contact sheet from `brief.reference_images` and attach that. Either way the real images must reach the model.

Save the generated image to the article's media directory using `brief.suggested_filename`:

```
<site-root>/backend-storefront/public/blog/<article-slug>/<suggested_filename>
```

### 3. Update the article

Open the article markdown and replace the `heroImage:` frontmatter value with `brief.frontmatter_patch`. Do NOT overwrite the existing image file with the same filename — use the new semantic name from the brief. Cache-busting is the whole point.

### 4. Verify locally

```bash
cd <site-root>
npm run build           # Next build with dummy env if needed
```

The build must succeed and the new image must resolve.

### 5. Commit, push, validate in production

```bash
git add backend-storefront/content/blog/<article>.md
git add backend-storefront/public/blog/<article-slug>/<suggested_filename>
git commit -m "Replace <article> hero with real product scene"
git push
```

Then run the validation checklist from `brief.validation_checklist`:

1. CI green on GitHub
2. Production article page returns 200 and HTML contains the new filename (not the old one)
3. Production blog list page shows the new hero card
4. The new image URL returns 200 with the expected size
5. Visual confirmation (screenshot) — products labeled, integrated, no collage

If production still shows the OLD image, the cause is one of:
- deploy hasn't completed → wait and retry
- CDN cache on the old filename → if you reused the old filename, this is why we use a new one
- frontmatter still points to the old file → re-check the patch landed

## Social Promotion Wave

A social promotion wave is a batch of post visuals (Instagram/Facebook square + story, OG preview, etc.) for an article. It uses the **same brief and the same discipline as the hero** — there is no separate, looser path for social.

1. Build the brief once (Procedure §1) for the article. Reuse its `products` and `reference_images`.
2. For **every** post in the wave, attach `brief.reference_images` (the real ARDmag packshots) to the Codex/imagegen call — exactly as for the hero. Vary only crop / aspect ratio / composition, never the packaging source.
   - Square post: `aspect_ratio="square"`
   - Story / vertical: `aspect_ratio="portrait"`
   - OG / link preview: `aspect_ratio="landscape"`, same scene as the hero (see `references/visual-rules.md` → "Same-source previews").
3. Require the real products to appear organically in each post image — on stone samples / workbench, packaging matching the references, no collage, no floating cutouts.
4. If `brief.reference_images` is empty, STOP — do not let the wave run from product names alone. That is precisely how the 2026-05-26 wave shipped real names (SEAL, QUASAR, WET SEAL, IDROREP, ECO STONE PRO) on AI-invented packaging.

Re-run the brief builder per article; do not hand-maintain a separate product list for social.

## Hard Rules (Non-Negotiable)

These rules are encoded in `prompts/hero_image.md` and applied by the brief builder. They exist because of real incidents (Delta Research hero and social wave, 2026-05-26) where each was violated:

1. **No collages.** No grid of cropped product shots. No cutouts pasted onto a background.
2. **No floating products.** No products hovering against a solid color or studio gradient.
3. **No unlabeled bottles.** If the article names specific Delta Research / Tenax products, the bottles in the scene must show those labels legibly.
4. **No wrong-brand products.** If the article is about Delta Research, do not put Tenax bottles in the scene.
5. **No generic-category-only mapping.** If the article names `SEAL`, `QUASAR`, `IDROREP` etc., the prompt must reference those specific products — not just "treatment products" or `solutii-delta`.
6. **No filename reuse.** When replacing a conceptually wrong hero, use a new semantic filename (e.g. `hero-delta-research-tratamente.webp`), not the old `hero.webp`.
7. **No "done" without production validation.** Build success is not the bar. The production HTML must reference the new filename and the image must render correctly.
8. **No generation without the real packshots attached.** For the hero, the OG preview, and every social-wave post, the real product images from `brief.reference_images` must be attached to the generator as references. Naming the products in the prompt is not enough — that is what made the social wave invent packaging for real product names.

## Pitfalls

- **Re-optimizing the old image instead of replacing it.** If the concept is wrong, optimization doesn't fix it. The brief builder flags articles where the current hero is named `hero.webp` / `hero.jpg` / `hero.png` (generic) as needing a semantic rename.
- **Stopping at "I pushed".** A push that doesn't propagate to production is not done.
- **Trusting frontmatter `tags` alone.** Tags may say "delta research" while the body names specific products. The detector reads both.
- **Latinized vs Romanian product names.** `Total Wet`, `Wet Seal`, `Eco Stone Pro` may appear with mixed casing. The detector normalizes case and diacritics.

## Verification

The image (hero or any social-wave post) is acceptable if:

- The brief's `products` list is non-empty and at least one entry has `asset_dir` populated
- `brief.reference_images` is non-empty and those real packshots were attached to the generation call
- The generated image shows the named products with **packaging matching the references** and readable labels, integrated on stone / workshop context, no collage
- `suggested_filename` differs from any existing hero filename for that article
- After push, production HTML contains `suggested_filename` and the image URL returns 200
