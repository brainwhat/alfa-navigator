"""Static Onest TTFs for the decision PDF, built from the web subsets in app/static/fonts.

The page loads Onest as two variable woff2 subsets (Cyrillic and Latin); a PDF needs one static
TrueType font per weight. Usage (fontTools and brotli are build-time only):
    uvx --with brotli --from fonttools python scripts/build_pdf_fonts.py
"""
import tempfile
from pathlib import Path

from fontTools.merge import Merger
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
SUBSETS = [ROOT / 'app/static/fonts/onest-latin.woff2', ROOT / 'app/static/fonts/onest-cyrillic.woff2']
WEIGHTS = {'Regular': 400, 'SemiBold': 600}
OUT = ROOT / 'app/fonts'

def main():
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, weight in WEIGHTS.items():
            parts = []
            for subset in SUBSETS:
                font = instancer.instantiateVariableFont(TTFont(subset), {'wght': weight})
                font.flavor = None
                path = Path(tmp) / f'{subset.stem}-{name}.ttf'
                font.save(path)
                parts.append(str(path))
            merged = Merger().merge(parts)
            target = OUT / f'Onest-{name}.ttf'
            merged.save(target)
            print(f'{target.relative_to(ROOT)}: {len(merged.getBestCmap())} символов')

if __name__ == '__main__':
    main()
