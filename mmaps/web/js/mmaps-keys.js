/**
 * MMapsKeys - Stream 1: API key / credential lifecycle.
 *
 * Contract (no other module may break this):
 *   • localStorage is written ONLY by save() / clearSaved().
 *   • test() never writes localStorage; only sets an in-memory session key.
 *   • After Test without Save, a full page reload has no key from this module.
 *   • getActiveKey(typed) is the only way other code learns "which key to use".
 *
 * Other modules MUST NOT read/write mmaps-google-maps-api-key themselves.
 */
(function (global) {
  'use strict';

  var LS_KEY = 'mmaps-google-maps-api-key';
  /** @type {string|null} in-memory only; cleared on reload */
  var sessionKey = null;

  function readSaved() {
    try {
      return (global.localStorage.getItem(LS_KEY) || '').trim();
    } catch (e) {
      return '';
    }
  }

  function writeSaved(key) {
    try {
      var k = (key || '').trim();
      if (k) global.localStorage.setItem(LS_KEY, k);
      else global.localStorage.removeItem(LS_KEY);
    } catch (e) {
      /* private mode / quota */
    }
  }

  var MMapsKeys = {
    /** Persisted key only (may be empty). */
    getSavedKey: function () {
      return readSaved();
    },

    /** Session key from last successful Test this page lifetime (may be null). */
    getSessionKey: function () {
      return sessionKey;
    },

    /**
     * Key for map use / toggle.
     * @param {string} [typedFromInput] current settings field value (staging)
     */
    getActiveKey: function (typedFromInput) {
      var typed = (typedFromInput || '').trim();
      if (typed) return typed;
      if (sessionKey) return sessionKey;
      return readSaved();
    },

    /** Value to show in the settings field when opening the panel. */
    getKeyForSettingsField: function () {
      return sessionKey || readSaved() || '';
    },

    /**
     * Validate key via inject validateFn(key) -> Promise<{ok, reason}>.
     * On success: sets sessionKey only. NEVER writes localStorage.
     * @param {string} key
     * @param {function(string): Promise<{ok:boolean, reason:string}>} validateFn
     */
    test: function (key, validateFn) {
      var k = (key || '').trim();
      if (!k) {
        return Promise.resolve({
          ok: false,
          reason: 'No API key entered. Paste your Google Maps key first.'
        });
      }
      if (typeof validateFn !== 'function') {
        return Promise.resolve({
          ok: false,
          reason: 'Internal error: no key validator provided.'
        });
      }
      return Promise.resolve()
        .then(function () {
          return validateFn(k);
        })
        .then(function (result) {
          if (result && result.ok) {
            sessionKey = k;
            return {
              ok: true,
              reason:
                (result.reason || 'Key works with Maps JavaScript API.') +
                ' Click Save to keep it after restart.'
            };
          }
          return {
            ok: false,
            reason: (result && result.reason) || 'Key validation failed.'
          };
        })
        .catch(function (err) {
          return {
            ok: false,
            reason: (err && err.message) ? err.message : String(err)
          };
        });
    },

    /**
     * ONLY path that persists across restarts.
     * Also updates sessionKey so the current session stays consistent.
     */
    save: function (key) {
      var k = (key || '').trim();
      writeSaved(k);
      sessionKey = k || null;
      if (k) {
        return {
          ok: true,
          reason: 'Saved on this Mac only. It will still be here after you close the app.'
        };
      }
      return {
        ok: true,
        reason: 'Cleared saved key. Nothing stored for next launch.'
      };
    },

    /** Clear disk + session (e.g. user saved empty string). */
    clearSaved: function () {
      writeSaved('');
      sessionKey = null;
    },

    /** Test helpers / diagnostics (do not use for map load). */
    _debugHasSession: function () {
      return !!sessionKey;
    },
    _debugStorageKeyName: function () {
      return LS_KEY;
    }
  };

  global.MMapsKeys = MMapsKeys;
})(typeof window !== 'undefined' ? window : globalThis);
