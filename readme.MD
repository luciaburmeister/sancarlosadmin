For more translations
django-admin makemessages -l es --no-location --no-wrap
django-admin compilemessages

## 🚀 Running the Project

### Django Development Server
```bash
python manage.py runserver
```

### PDF generation (WeasyPrint)

Django starts without loading WeasyPrint; PDFs are generated on demand. The server still needs native libraries:

- **Linux (production / Docker)**: install Pango, Cairo, GDK-Pixbuf (e.g. Debian: `libpango-1.0-0`, `libcairo2`, `libgdk-pixbuf-2.0-0`).
- **macOS (development)**:
  ```bash
  brew install pango cairo gdk-pixbuf glib
  export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:${DYLD_FALLBACK_LIBRARY_PATH:-}"
  python manage.py runserver
  ```
  Prefer `python manage.py` over `./manage.py` so the shell can pass `DYLD_*` to Python. If PDF export fails with `libgobject`, use `python manage.py` or set `DYLD_FALLBACK_LIBRARY_PATH` in `~/.zshrc`.

### Celery Background Tasks
The project uses Celery for background tasks and Redis as the message broker.

1. **Start Redis**:
   ```bash
   # On Mac with Homebrew
   brew services start redis

   ```
2. **Run Celery Worker**:
   ```bash
   celery -A alumniniooerp worker -l info --pool=solo
   ```

3. **Run Celery Beat** (required for scheduled tasks):
   ```bash
   celery -A alumniniooerp beat -l info
   ```

Locally the broker defaults to `redis://localhost:6379/0`; set `CELERY_BROKER_URL` to point elsewhere.

## Deployment (Elastic Beanstalk)

Production runs as two Elastic Beanstalk environments in the same application, built from the same commit:

| Environment | Role | Runs |
|---|---|---|
| `Aluminioerp-prod2` | `web` | gunicorn, nginx, collectstatic, migrations |
| `polipus-workers` | `worker` | Celery default worker, pdf worker, beat (single instance), plus a one-worker gunicorn for EB health checks |

The broker is an ElastiCache Valkey node, reached through `CELERY_BROKER_URL` (a GitHub secret written into `.env` by CI). Nothing else is shared between the two instances: media is on S3, task results and the beat schedule are in Postgres.

Each role's EB files live under `deploy/`:

```
deploy/common/   .ebextensions shared by every role (native packages, collectstatic)
deploy/web/      Procfile, nginx upload limit, migrations
deploy/worker/   Procfile (health-check gunicorn), swap and Celery systemd hooks
```

CI runs `scripts/deploy/apply_overlay.sh <role>` to copy `common/` and `<role>/` over the repo root, then `eb deploy`, web first. The repo root itself has no `.ebextensions`, `.platform` or `Procfile` committed; run the script locally with a role if you need to `eb deploy` by hand.


⚙️ How it all works together
Step 1

Company defines a ProductTemplate.

Step 2

Company defines parameters for that product (width / color / etc).

Step 3

Company defines BOM rules (like in the Excel formulas).

Step 4

User creates a Configuration and selects a ProductTemplate.

Step 5

User fills parameters → ConfigurationParameterValue.

Step 6

BOM rules generate ConfigurationItems.

Step 7

Pricing rules generate final price.

Step 8

Optional: convert to Quote → Order.


Formula Language (for rules & calculations)

In several places in the system (BOM rules, pricing, conditions…), you can write formulas instead of fixed values.

A formula is a small expression like:

width * height * 1.05
ceil(area * boxes_per_m2)
1.1 if material == "cotton" else 1.05


The system evaluates these formulas using the values of the variables (width, height, material, etc.) for each item.

This document explains what you can do in a formula.

1. What you can use in a formula

You can use:

Numbers

Examples: 1, 0.5, 10.75

Text (strings)

Always inside quotes: "cotton", "white", "floor", 'polyester'

Booleans

True or False (rarely needed for non-technical users, but allowed)

Variables
These are provided by the system (for example):

width

height

area

quantity

material

color

boxes_per_m2

The exact list of available variables depends on where the formula is used (product, line item, BOM, etc.).
Use the variable names exactly as shown in the system (case sensitive).

2. Basic math

You can do simple arithmetic, like in a calculator:

Symbol	Meaning	Example
+	addition	width + 5
-	subtraction	height - 0.2
*	multiplication	width * height
/	division	area / 2
%	modulo (rest)	quantity % 3
**	power	width ** 2 (width squared)

Examples:

width * height
area * 1.05          # add 5% waste
width * height * 2   # double area


(You don’t need the comments; they’re just explanations here.)

3. Comparisons (conditions)

You can compare values in your formula, for example to check material, sizes, etc.

Symbol	Meaning	Example
==	equals	material == "cotton"
!=	not equal	color != "white"
<	less than	width < 2
<=	less than or equal	width <= 1.5
>	greater than	area > 3
>=	greater than or equal	area >= 2
"x" in y	contains	"cot" in material
"x" not in y	does not contain	"red" not in color

Examples:

material == "cotton"
width > 2
"cot" in material
color != "white"


You can also chain comparisons:

1 < width < 2.5

4. AND / OR (combine conditions)

You can combine multiple conditions with and and or:

and → both conditions must be true

or → at least one must be true

Examples:

material == "cotton" and width > 1
(material == "cotton" or material == "linen") and area > 2
"white" in color and area <= 3


You can also use parentheses (...) to group conditions clearly.

5. If–then–else inside a formula

You can write an inline condition:

A if CONDITION else B


Think of it as:

“If CONDITION is true, use A, otherwise use B”.

Examples:

# Waste factor depending on material
1.1 if material == "cotton" else 1.05

# Different quantity rule depending on area
ceil(area * boxes_per_m2) if area > 2 else 1

# Extra charge only for large items
price * 1.1 if width > 2 else price


This is the main way to express “if this, then that” in formulas.

6. Rounding functions

You can use a small set of built-in functions:

round(x) – normal rounding

Rounds to the nearest value:

round(area * 1.08)
round(width * height, 2)    # round to 2 decimal places


Both of these are allowed:

round(x)

round(x, number_of_decimals)

ceil(x) – round up (always)

Always round up to the next whole number:

ceil(area * boxes_per_m2)    # round up number of boxes
ceil(quantity / units_per_box)


Useful when you can’t supply half a box / roll / pack.

floor(x) – round down

Always round down (towards zero):

floor(area)
floor(length_in_meters)

7. Useful real-world examples
Example 1 – Boxes needed for a floor
ceil(area * boxes_per_m2)


“Number of boxes is area multiplied by boxes per m², rounded up.”

Example 2 – Different waste factor per material
1.1 if material == "cotton" else 1.05


“Use 10% extra if cotton, otherwise 5% extra.”

Example 3 – Add a minimum quantity
ceil(area * units_per_m2) if area > 2 else 1


“If area is more than 2 m², calculate normally; otherwise at least 1 unit.”

Example 4 – Check if color contains a word
1.2 if "white" in color else 1.0


“Apply a 20% factor when color includes the word white.”

Example 5 – Combine conditions
1.15 if material == "cotton" and area > 3 else 1.05


“If material is cotton and area is larger than 3 m² → 15% extra; otherwise 5% extra.”

8. Things you cannot do

To keep formulas safe and predictable, some things are not allowed:

No saving values:
❌ x = width * height

No calling methods:
❌ material.upper()
❌ math.sqrt(area)

No lists or dictionaries:
❌ [1, 2, 3]
❌ {"a": 1}

No loops or big programs:
❌ for, while, if: with new lines

No function names other than:

round(...)

ceil(...)

floor(...)

If you try to use something not allowed, the system will mark the formula as invalid and show an error message.

9. Tips for non-technical users

Start simple: width * height or area * 1.05.

Add conditions gradually:
area * 1.05 → area * 1.05 if material == "cotton" else area.

Always put text inside quotes:
material == "cotton", not material == cotton.

Use ceil(...) when you cannot ship partial units (boxes, rolls, packs).

If a formula fails, check for:

Typos in variable names (width vs widht).

Missing quotes around text.

Missing else in an if ... else ... expression.