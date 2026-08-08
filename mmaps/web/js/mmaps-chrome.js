/**
 * MMapsChrome — Stream 3: panel open/close + chrome stacking helpers.
 *
 * Contract:
 *   • Panel visibility uses ONE source of truth: CSS class `open` only.
 *     Inline style.display is set only as a mirror for older CSS fallbacks,
 *     always written together with the class (never independently).
 *   • isOpen() reads the class, not a second competing flag.
 *   • No map provider knowledge here.
 */
(function (global) {
  'use strict';

  function $(id) {
    return global.document.getElementById(id);
  }

  var MMapsChrome = {
    /**
     * @param {string} id element id
     * @param {boolean} open
     * @param {string} [displayWhenOpen='block'] 'block' | 'flex'
     */
    setPanel: function (id, open, displayWhenOpen) {
      var el = $(id);
      if (!el) return;
      var show = displayWhenOpen || 'block';
      if (open) {
        el.classList.add('open');
        el.style.display = show;
        el.setAttribute('data-mmaps-open', '1');
        el.style.pointerEvents = 'auto';
      } else {
        el.classList.remove('open');
        el.style.display = 'none';
        el.removeAttribute('data-mmaps-open');
        el.style.pointerEvents = 'none';
      }
    },

    isPanelOpen: function (id) {
      var el = $(id);
      if (!el) return false;
      // Single source of truth: class `open` (data attr mirrors it).
      return el.classList.contains('open');
    },

    anyFloatPanelOpen: function () {
      return MMapsChrome.isPanelOpen('findPanel') || MMapsChrome.isPanelOpen('goPanel');
    },

    /**
     * Bind a segment control (mode/speed) with clean click handling.
     * No preventDefault (pywebview/WebKit can drop the activation).
     */
    bindSegment: function (rootId, attr, handler) {
      var root = $(rootId);
      if (!root) return;
      root.addEventListener('click', function (e) {
        var btn = e.target.closest('button[' + attr + ']');
        if (!btn || !root.contains(btn) || btn.disabled) return;
        e.stopPropagation();
        handler(btn.getAttribute(attr));
      });
    }
  };

  global.MMapsChrome = MMapsChrome;
})(typeof window !== 'undefined' ? window : globalThis);
