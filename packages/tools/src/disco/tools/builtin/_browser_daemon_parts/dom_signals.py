"""DOM-analysis helpers, split out of ``_browser_daemon``.

They read no instance state -- they take a page and return what a human
would actually see -- so they were module functions living in the daemon
module, and their large embedded JS templates pushed the module (and, for
``_count_visible_semantic_elements``, the callable itself) over budget. The
JS is data, not control flow; hoisting it to a module constant is a genuine
reduction of the callable's own logical size, not a relocated violation.

Re-exported via a redundant alias so ``daemon_mod._visible_dom_text`` /
``daemon_mod._count_visible_semantic_elements`` (imported directly by tests)
keep resolving exactly as before.
"""

from __future__ import annotations

_VISIBLE_DOM_TEXT_JS = r"""
    () => {
        const output = [];
        const transparent = value => value === 'transparent' ||
            /^rgba\(.*,[ ]*0(?:\.0+)?\)$/.test(value);
        // A transparent fill over a background CLIPPED TO TEXT is
        // the gradient-heading idiom: the glyphs are painted by the
        // background and ARE visible. Transparent fill WITHOUT that
        // clip is still cloaked copy and stays excluded.
        const paintsTextViaBackground = style =>
            (style.webkitBackgroundClip === 'text' ||
             style.backgroundClip === 'text') &&
            (style.backgroundImage || 'none') !== 'none';
        const hiddenText = style =>
            (transparent(style.color) ||
             transparent(style.webkitTextFillColor || '')) &&
            !paintsTextViaBackground(style);
        const intersects = (first, second) =>
            Math.min(first.right, second.right) >
                Math.max(first.left, second.left) &&
            Math.min(first.bottom, second.bottom) >
                Math.max(first.top, second.top);
        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT
        );
        while (walker.nextNode()) {
            const node = walker.currentNode;
            const raw = (node.nodeValue || '').replace(/\s+/g, ' ').trim();
            if (!raw) continue;
            const parent = node.parentElement;
            if (!parent || parent.closest('[aria-hidden="true"]')) continue;

            let visible = true;
            const clippingAncestors = [];
            for (let current = parent; current; current = current.parentElement) {
                const style = window.getComputedStyle(current);
                if (style.display === 'none' ||
                    style.visibility === 'hidden' ||
                    style.visibility === 'collapse' ||
                    Number(style.opacity) === 0 ||
                    hiddenText(style) ||
                    style.clipPath === 'inset(100%)') {
                    visible = false;
                    break;
                }
                if (/(hidden|clip|scroll|auto)/.test(
                    `${style.overflow} ${style.overflowX} ${style.overflowY}`
                )) {
                    clippingAncestors.push(current.getBoundingClientRect());
                }
            }
            if (!visible) continue;

            const range = document.createRange();
            range.selectNodeContents(node);
            const rendered = Array.from(range.getClientRects()).some(rect => {
                if (rect.width <= 0 || rect.height <= 0) return false;
                return clippingAncestors.every(clip => intersects(rect, clip));
            });
            if (rendered) output.push(raw);
        }
        return output.join('\n').slice(0, 4000);
    }
"""

_SEMANTIC_ELEMENTS_JS = r"""
    () => {
        const viewportWidth = window.innerWidth ||
            document.documentElement.clientWidth;
        const viewportHeight = window.innerHeight ||
            document.documentElement.clientHeight;
        const transparent = value => value === 'transparent' ||
            /^rgba\(.*,[ ]*0(?:\.0+)?\)$/.test(value);
        // A transparent fill over a background CLIPPED TO TEXT is
        // the gradient-heading idiom: the glyphs are painted by the
        // background and ARE visible. Transparent fill WITHOUT that
        // clip is still cloaked copy and stays excluded.
        const paintsTextViaBackground = style =>
            (style.webkitBackgroundClip === 'text' ||
             style.backgroundClip === 'text') &&
            (style.backgroundImage || 'none') !== 'none';
        const hiddenText = style =>
            (transparent(style.color) ||
             transparent(style.webkitTextFillColor || '')) &&
            !paintsTextViaBackground(style);
        const intersect = (box, left, top, right, bottom) => ({
            left: Math.max(box.left, left),
            top: Math.max(box.top, top),
            right: Math.min(box.right, right),
            bottom: Math.min(box.bottom, bottom),
        });
        return Array.from(document.querySelectorAll(
            'h1, h2, h3, h4, h5, h6'
        )).filter(el => {
            if (typeof el.checkVisibility === 'function' &&
                !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) {
                return false;
            }
            const style = window.getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none' ||
                style.pointerEvents === 'none' || Number(style.opacity) === 0 ||
                hiddenText(style)) {
                return false;
            }

            // A DOM box can exist while all of its text is clipped. Walk
            // actual text ranges, then intersect each range with the
            // viewport and every clipping ancestor.
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            const textNodes = [];
            while (walker.nextNode()) {
                if ((walker.currentNode.textContent || '').trim()) {
                    textNodes.push(walker.currentNode);
                }
            }
            return textNodes.some(node => {
                const textParent = node.parentElement || el;
                if (typeof textParent.checkVisibility === 'function' &&
                    !textParent.checkVisibility({
                        checkOpacity: true, checkVisibilityCSS: true
                    })) {
                    return false;
                }
                const nodeStyle = window.getComputedStyle(textParent);
                if (nodeStyle.visibility === 'hidden' ||
                    nodeStyle.display === 'none' ||
                    nodeStyle.pointerEvents === 'none' ||
                    Number(nodeStyle.opacity) === 0 ||
                    hiddenText(nodeStyle)) {
                    return false;
                }
                const range = document.createRange();
                range.selectNodeContents(node);
                return Array.from(range.getClientRects()).some(rect => {
                    if (rect.width <= 0 || rect.height <= 0) return false;
                    let box = intersect(
                        rect, 0, 0, viewportWidth, viewportHeight
                    );
                    for (let ancestor = textParent; ancestor;
                         ancestor = ancestor.parentElement) {
                        const ancestorStyle = window.getComputedStyle(ancestor);
                        if (ancestorStyle.clipPath !== 'none' ||
                            ancestorStyle.clip !== 'auto') {
                            return false; // clipping geometry is not safely inferable
                        }
                        const ancestorRect = ancestor.getBoundingClientRect();
                        if (ancestorStyle.overflowX !== 'visible') {
                            box.left = Math.max(box.left, ancestorRect.left);
                            box.right = Math.min(box.right, ancestorRect.right);
                        }
                        if (ancestorStyle.overflowY !== 'visible') {
                            box.top = Math.max(box.top, ancestorRect.top);
                            box.bottom = Math.min(box.bottom, ancestorRect.bottom);
                        }
                    }
                    const width = box.right - box.left;
                    const height = box.bottom - box.top;
                    const visibleRatio = (width * height) / (rect.width * rect.height);
                    if (width < 2 || height < 2 || visibleRatio < 0.25) return false;

                    // Geometry alone cannot prove the text is not fully
                    // covered by another element. At least one sampled
                    // point must resolve to the text parent or one of its
                    // ancestors (never an opaque covering descendant).
                    const points = [
                        [0.5, 0.5], [0.25, 0.25], [0.75, 0.25],
                        [0.25, 0.75], [0.75, 0.75],
                    ];
                    return points.some(([x, y]) => {
                        const hit = document.elementFromPoint(
                            box.left + width * x, box.top + height * y
                        );
                        return hit === textParent ||
                            (hit !== null && hit.contains(textParent));
                    });
                });
            });
        }).length;
    }
"""


def _visible_dom_text(page) -> str:
    """Return bounded exact text from rendered, non-hidden DOM text nodes.

    ``innerText`` is the right user-facing summary, but browsers apply CSS
    ``text-transform`` to it. Exact content claims also need the authored
    node text, coupled to browser-owned visibility and geometry rather than
    raw HTML/source bytes. Hidden, transparent, clipped, zero-area, and
    ``aria-hidden`` nodes are excluded.
    """
    try:
        value = page.evaluate(_VISIBLE_DOM_TEXT_JS)
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _count_visible_semantic_elements(page) -> int:
    """Count rendered, content-bearing DOM semantics.

    The interactive element walker deliberately ignores ordinary headings and
    paragraphs.  Without a separate signal, a valid small page such as
    ``<h1>Live Server Up</h1>`` is indistinguishable from an empty SPA shell to
    the finish and verification gates.  Keep this signal deliberately narrow:
    only headings count.  Short paragraphs/list items can be transient loading
    placeholders, and unpainted canvas or broken media must not manufacture a
    pass.  Hidden, zero-area, and off-viewport headings do not count, and a
    heading must contain non-whitespace text.
    """
    try:
        count = page.evaluate(_SEMANTIC_ELEMENTS_JS)
    except Exception:
        return 0
    # JavaScript data is untrusted.  In particular, bool is an int subclass in
    # Python and must not become a fabricated positive count.
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return 0
    return count


def get_elements(page, *, max_elements: int):
    """Former ``BrowserHandler._get_elements``.

    W6: broaden the element walker beyond standard interactive elements.
    We now also index:
      - <div>, <span>, <li> elements that have onclick handlers (already
        covered by [onclick] but this ensures we capture them with the
        extended selector set)
      - elements whose computed CSS cursor is 'pointer' (the idiomatic
        signal for "this is clickable" in modern SPAs and design-system
        components that use divs as buttons)
    The walker assigns data-pmx-index to each found element so the
    click action can reach it by index without knowing the CSS path.
    """
    return page.evaluate(f"""
        () => {{
            // Standard interactive elements — always indexed.
            const standardSelectors = [
                'a', 'button', 'input', 'select', 'textarea',
                '[role="button"]', '[onclick]'
            ];
            const standardSet = new Set(
                Array.from(document.querySelectorAll(standardSelectors.join(',')))
            );

            // Extended: block/inline elements with cursor:pointer that aren't
            // already covered by the standard set.
            const extendedTags = ['div', 'span', 'li', 'td', 'th', 'label',
                                  'article', 'section', 'header', 'nav', 'aside'];
            const pointerCandidates = Array.from(
                document.querySelectorAll(extendedTags.join(','))
            ).filter(el => {{
                if (standardSet.has(el)) return false;  // already in standard set
                return window.getComputedStyle(el).cursor === 'pointer';
            }});

            // Union: standard first (preserve ordering), then pointer extras.
            const allElements = [
                ...Array.from(standardSet),
                ...pointerCandidates,
            ]
            .filter(el => {{
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 &&
                       window.getComputedStyle(el).visibility !== 'hidden' &&
                       window.getComputedStyle(el).display !== 'none';
            }})
            .slice(0, {max_elements});

            return allElements.map((el, i) => {{
                const index = i + 1;
                el.setAttribute('data-pmx-index', index.toString());
                const tag = el.tagName.toLowerCase();
                let text = (el.innerText || el.textContent || el.value ||
                            el.placeholder || "").trim().replace(/\\n/g, " ");
                if (text.length > 80) text = text.substring(0, 77) + "...";
                return `${{index}}[:] <${{tag}}>${{text}}</${{tag}}>`;
            }});
        }}
    """)
