"""Shrink the HGIS base maps (territory PNGs) for hosting outside the app server.

    py -3 transform/compress_basemaps.py                  (run from apps_as_factory/)
    py -3 transform/compress_basemaps.py --sample 40      try 40 images first
    py -3 transform/compress_basemaps.py --mode lossless  pixel-identical WebP only

Input  data/mapas_base/<WikiID>_<year>.png   (e.g. abancay_jurisdiccion_per_1750.png;
       subfolders are fine, the folder structure is mirrored)
Output data/mapas_base_web/<same name>.webp  plus  data/mapas_base_web/manifest.json

The originals are never modified. Re-running skips images whose output is
already newer than the source, so an interrupted run can simply be restarted
(--force converts everything again).

Modes
  lossy     (default) WebP, --quality 85. On the HGIS maps (relief shading,
            labels) about 75 % smaller and visually identical; ~0.3 s per map.
  auto      encodes lossless and lossy and keeps the smaller; only worth it for
            flat-colour maps, and ~10x slower (lossless WebP is slow).
  lossless  WebP, pixel-identical to the original (only ~35 % smaller here).
  png       Quantised PNG (--colors 256, optimised): for hosts or tools that
            need PNG (~50 % smaller here).
--max-side N additionally scales images down so the longer side is at most N px.

manifest.json lists, per territory WikiID, the years that have a map, so the
territory page can link only the years that exist:
  {"ext": ".webp", "maps": {"abancay_jurisdiccion_per": [1701, 1725, ...], ...}}
"""
import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from PIL import Image

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # apps_as_factory
SRC_DEFAULT = os.path.join(BASE, 'data', 'mapas_base')
OUT_DEFAULT = os.path.join(BASE, 'data', 'mapas_base_web')
IMG_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.tif', '.tiff', '.bmp')
NAME_RX = re.compile(r'^(?P<id>.+)_(?P<year>\d{4})$')


def out_ext(mode):
    return '.png' if mode == 'png' else '.webp'


def convert_one(src, dst, mode, quality, colors, max_side):
    """Convert one image; returns (src, in_bytes, out_bytes, error)."""
    tmp, alt = dst + '.part', dst + '.alt.part'
    try:
        size_in = os.path.getsize(src)
        with Image.open(src) as im:
            im.load()
            has_alpha = im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info)
            im = im.convert('RGBA' if has_alpha else 'RGB')
            if max_side and max(im.size) > max_side:
                im.thumbnail((max_side, max_side), Image.LANCZOS)
            os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
            if mode == 'png':
                q = im.quantize(colors=colors, method=Image.FASTOCTREE if has_alpha else Image.MEDIANCUT,
                                dither=Image.Dither.NONE)
                q.save(tmp, format='PNG', optimize=True)
            elif mode == 'lossless':
                im.save(tmp, format='WEBP', lossless=True, quality=100, method=6)
            elif mode == 'lossy':
                im.save(tmp, format='WEBP', quality=quality, method=6, alpha_quality=100)
            else:                                             # auto: keep the smaller
                im.save(tmp, format='WEBP', lossless=True, quality=100, method=6)
                im.save(alt, format='WEBP', quality=quality, method=6, alpha_quality=100)
                if os.path.getsize(alt) < os.path.getsize(tmp):
                    os.replace(alt, tmp)
                else:
                    os.remove(alt)
        os.replace(tmp, dst)
        return src, size_in, os.path.getsize(dst), None
    except Exception as exc:                                  # keep going, report at the end
        for leftover in (tmp, alt):
            try:
                os.remove(leftover)
            except OSError:
                pass
        return src, 0, 0, '{}: {}'.format(type(exc).__name__, exc)


def find_images(src_root):
    for dirpath, _dirs, files in os.walk(src_root):
        for f in sorted(files):
            if f.lower().endswith(IMG_EXT):
                yield os.path.join(dirpath, f)


def manifest_key(path):
    """'.../abancay_jurisdiccion_per_1750.webp' -> ('abancay_jurisdiccion_per', 1750)."""
    m = NAME_RX.match(os.path.splitext(os.path.basename(path))[0])
    return (m.group('id'), int(m.group('year'))) if m else None


def level_of(path):
    """Territory level from the WikiID (<name>_<level>_<region>), for the report."""
    key = manifest_key(path)
    parts = key[0].split('_') if key else []
    return parts[-2] if len(parts) >= 3 else '(other)'


def mb(n):
    return n / 1048576


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--src', default=SRC_DEFAULT, help='input root (default data/mapas_base)')
    ap.add_argument('--out', default=OUT_DEFAULT, help='output root (default data/mapas_base_web)')
    ap.add_argument('--mode', choices=('lossy', 'auto', 'lossless', 'png'), default='lossy')
    ap.add_argument('--quality', type=int, default=85, help='WebP quality for the lossy encoding (default 85)')
    ap.add_argument('--colors', type=int, default=256, help='palette size for --mode png (default 256)')
    ap.add_argument('--max-side', type=int, default=0, help='scale down so the longer side is at most N px')
    ap.add_argument('--sample', type=int, default=0, help='only N randomly chosen images (test run)')
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument('--force', action='store_true', help='reconvert even if the output is up to date')
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        sys.exit('Input folder not found: {}'.format(args.src))
    ext = out_ext(args.mode)

    sources = list(find_images(args.src))
    if args.sample:
        random.seed(0)
        sources = random.sample(sources, min(args.sample, len(sources)))
    jobs, skipped = [], 0
    for src in sources:
        rel = os.path.relpath(src, args.src)
        dst = os.path.join(args.out, os.path.splitext(rel)[0] + ext)
        if (not args.force and os.path.exists(dst)
                and os.path.getmtime(dst) >= os.path.getmtime(src)):
            skipped += 1
            continue
        jobs.append((src, dst))

    print('{} images to convert ({} already up to date), mode={}{}, {} workers'.format(
        len(jobs), skipped, args.mode,
        ' q={}'.format(args.quality) if args.mode in ('auto', 'lossy') else '', args.workers))

    totals = {}                                # level -> [count, in, out]
    errors = []
    t0 = time.time()
    with ProcessPoolExecutor(args.workers) as ex:
        futs = [ex.submit(convert_one, s, d, args.mode, args.quality, args.colors, args.max_side)
                for s, d in jobs]
        for n, fut in enumerate(as_completed(futs), 1):
            src, size_in, size_out, err = fut.result()
            if err:
                errors.append((src, err))
            else:
                t = totals.setdefault(level_of(src), [0, 0, 0])
                t[0] += 1
                t[1] += size_in
                t[2] += size_out
            if n % 200 == 0 or n == len(futs):
                print('  {}/{}  ({:.0f}s)'.format(n, len(futs), time.time() - t0), flush=True)

    # manifest from everything now present in the output tree (incl. earlier runs)
    maps, odd = {}, []
    for dirpath, _dirs, files in os.walk(args.out):
        for f in files:
            if not f.endswith(ext):
                continue
            key = manifest_key(f)
            if key:
                maps.setdefault(key[0], []).append(key[1])
            else:
                odd.append(f)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump({'ext': ext, 'maps': {k: sorted(v) for k, v in sorted(maps.items())}},
                  f, ensure_ascii=False, indent=0)

    if totals:
        print('\n{:<16} {:>7} {:>10} {:>10} {:>7}'.format('level', 'images', 'in MB', 'out MB', 'saved'))
        all_in = all_out = all_n = 0
        for level, (n, si, so) in sorted(totals.items(), key=lambda kv: -kv[1][1]):
            print('{:<16} {:>7} {:>10.1f} {:>10.1f} {:>6.0f}%'.format(level, n, mb(si), mb(so),
                                                                     100 * (1 - so / si) if si else 0))
            all_n, all_in, all_out = all_n + n, all_in + si, all_out + so
        print('{:<16} {:>7} {:>10.1f} {:>10.1f} {:>6.0f}%'.format('TOTAL', all_n, mb(all_in), mb(all_out),
                                                                 100 * (1 - all_out / all_in) if all_in else 0))
    print('\nmanifest: {} maps of {} territories -> {}'.format(
        sum(len(v) for v in maps.values()), len(maps), os.path.join(args.out, 'manifest.json')))
    if odd:
        print('{} files not named <WikiID>_<year> (left out of the manifest): {}'.format(
            len(odd), ', '.join(odd[:10])))
    if errors:
        print('\n{} images FAILED:'.format(len(errors)))
        for src, err in errors[:50]:
            print('  {}  {}'.format(os.path.relpath(src, args.src), err))
        sys.exit(1)


if __name__ == '__main__':
    main()
