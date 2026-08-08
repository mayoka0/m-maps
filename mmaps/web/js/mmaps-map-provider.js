/**
 * MMapsProvider — Stream 2: MapLibre ↔ Google basemap switching.
 *
 * Contract:
 *   • Only this module creates/destroys google.maps.Map or loads the JS API script.
 *   • Only switchToGoogle(key) / switchToMapLibre() / toggle(key) change provider.
 *   • Google layer is never interactive until fully ready (gmap-active).
 *   • Failed load fully tears down #gmap so the next attempt is a genuine retry
 *     (script may stay cached for the same key; map instance is always fresh).
 *   • Does not read/write API key storage — caller passes key from MMapsKeys.
 *   • Provider control lives in the header (#mapProviderSeg); never reparented
 *     into the map / zoom stack.
 *   • Google map type (roadmap/satellite/hybrid/terrain) is owned here only.
 */
(function (global) {
  'use strict';

  function $(id) {
    return global.document && global.document.getElementById
      ? global.document.getElementById(id)
      : null;
  }

  var MAP_TYPES = { roadmap: 1, satellite: 1, hybrid: 1, terrain: 1 };

  /** @type {'maplibre'|'google'} */
  var provider = 'maplibre';
  /** @type {google.maps.Map|null} */
  var gmap = null;
  /** @type {maplibregl.Map|null} */
  var mlMap = null;
  var scriptPromise = null;
  var scriptKey = null;
  var switching = false;
  var clickHandler = null;
  /** Optional callback after successful switch: (provider) => void */
  var onProviderChange = null;
  /** @type {'roadmap'|'satellite'|'hybrid'|'terrain'} */
  var mapTypeId = 'roadmap';

  /** Smallest Google zoom whose 256px world is at least the viewport width. */
  function minimumWorldZoom(host) {
    var width = Math.max(1, (host && host.clientWidth) || global.innerWidth || 1);
    return Math.max(2, Math.ceil(Math.log(width / 256) / Math.LN2));
  }

  function enforceGoogleWorldZoom() {
    var host = $('gmap');
    if (!gmap || !host) return;
    var minimum = minimumWorldZoom(host);
    gmap.setOptions({ minZoom: minimum });
    if (Number(gmap.getZoom()) < minimum) gmap.setZoom(minimum);
  }

  function looksBroken(el) {
    if (!el) return true;
    var text = el.innerText || el.textContent || '';
    if (/Oops!\s*Something went wrong/i.test(text)) return true;
    if (/didn't load Google Maps correctly/i.test(text)) return true;
    if (el.querySelector('.gm-err-container, .gm-err-message, .gm-err-title')) return true;
    return false;
  }

  function setLayer(mode) {
    // mode: 'off' | 'loading' | 'on'
    var el = $('gmap');
    var ml = $('map');
    if (!el) return;
    el.classList.remove('gmap-loading', 'gmap-active');
    if (ml) ml.classList.remove('ml-suppressed');
    if (mode === 'loading') {
      el.classList.add('gmap-loading');
      el.setAttribute('aria-hidden', 'true');
    } else if (mode === 'on') {
      el.classList.add('gmap-active');
      el.setAttribute('aria-hidden', 'false');
      if (ml) ml.classList.add('ml-suppressed');
    } else {
      el.setAttribute('aria-hidden', 'true');
    }
  }

  function destroyGoogleMap() {
    if (gmap) {
      try {
        global.google.maps.event.clearInstanceListeners(gmap);
      } catch (e) {}
      gmap = null;
    }
    var el = $('gmap');
    if (el) el.innerHTML = '';
    setLayer('off');
  }

  /**
   * Load Maps JS API for key. Same key can be retried after map teardown.
   * Different key after success requires full page reload (Google limitation).
   */
  function loadScript(key) {
    key = (key || '').trim();
    if (!key) return Promise.reject(new Error('No API key entered.'));

    // Google only allows one Maps JS bootstrap per page lifetime.
    // Recovery is a page reload (fresh script context) — not a full app quit.
    if (global.google && global.google.maps) {
      if (scriptKey && scriptKey !== key) {
        var switchErr = new Error(
          'A different Google Maps key is already loaded on this page. Google does not allow switching keys without a fresh page. Click “Reload page” to load the new key (the app stays open).'
        );
        switchErr.mmapsCode = 'KEY_SWITCH_RELOAD';
        return Promise.reject(switchErr);
      }
      // Same key, or script arrived after a timed-out fail left scriptKey null.
      scriptKey = key;
      return Promise.resolve(global.google.maps);
    }
    if (scriptPromise) return scriptPromise;

    scriptPromise = new Promise(function (resolve, reject) {
      var settled = false;
      function fail(msg) {
        if (settled) return;
        settled = true;
        scriptPromise = null;
        // Allow retry of the same key after a failed bootstrap.
        scriptKey = null;
        reject(new Error(msg));
      }
      function ok() {
        if (settled) return;
        settled = true;
        scriptKey = key;
        resolve(global.google.maps);
      }

      global.gm_authFailure = function () {
        fail(
          'Google rejected this API key. Enable Maps JavaScript API, enable billing, and set Application restrictions to None (website referrer locks break this desktop app).'
        );
      };

      var cb = '__mmapsGmapsReady_' + String(Date.now());
      global[cb] = function () {
        try {
          delete global[cb];
        } catch (e) {}
        if (!(global.google && global.google.maps)) {
          fail('Google Maps script loaded but google.maps is missing.');
          return;
        }
        ok();
      };

      var script = global.document.createElement('script');
      script.async = true;
      script.defer = true;
      script.src =
        'https://maps.googleapis.com/maps/api/js?key=' +
        encodeURIComponent(key) +
        '&v=weekly&callback=' +
        cb;
      script.onerror = function () {
        fail('Failed to load the Google Maps script. Check network connectivity.');
      };
      global.document.head.appendChild(script);

      setTimeout(function () {
        if (!settled) {
          if (global.google && global.google.maps) ok();
          else fail('Timed out loading Google Maps. Check the key and network.');
        }
      }, 15000);
    });
    return scriptPromise;
  }

  /**
   * Probe with a temporary off-screen host (NOT #gmap).
   * Host must be layout-visible to Google (not visibility:hidden).
   */
  function probeRender() {
    return new Promise(function (resolve) {
      var host = global.document.createElement('div');
      host.setAttribute('aria-hidden', 'true');
      host.style.cssText =
        'position:fixed;left:-12000px;top:0;width:400px;height:300px;opacity:0;pointer-events:none;';
      global.document.body.appendChild(host);

      var settled = false;
      function finish(ok, reason) {
        if (settled) return;
        settled = true;
        try {
          host.remove();
        } catch (e) {}
        resolve({ ok: ok, reason: reason });
      }

      var probeMap;
      try {
        probeMap = new global.google.maps.Map(host, {
          center: { lat: 37.4, lng: -122.1 },
          zoom: 8,
          disableDefaultUI: true,
          keyboardShortcuts: false,
          draggable: false,
          clickableIcons: false,
          gestureHandling: 'none'
        });
      } catch (e) {
        finish(false, (e && e.message) ? e.message : String(e));
        return;
      }

      var prevAuth = global.gm_authFailure;
      global.gm_authFailure = function () {
        finish(
          false,
          'Google rejected this API key while rendering. Enable Maps JavaScript API + billing; Application restrictions must be None for M Maps.'
        );
        if (typeof prevAuth === 'function') {
          try {
            prevAuth();
          } catch (e) {}
        }
      };

      global.google.maps.event.addListenerOnce(probeMap, 'tilesloaded', function () {
        setTimeout(function () {
          if (looksBroken(host)) {
            finish(
              false,
              'Google Maps showed an error page. Check billing, Maps JavaScript API, and Application restrictions = None.'
            );
          } else {
            finish(true, 'Key works — map tiles load.');
          }
        }, 300);
      });

      setTimeout(function () {
        if (looksBroken(host)) {
          finish(
            false,
            'Google Maps failed to render (Oops). Usually: billing, Maps JavaScript API, or Application restrictions (use None on desktop).'
          );
        } else if (host.querySelector('canvas, img, .gm-style')) {
          finish(true, 'Key works — map surface appeared.');
        } else {
          finish(false, 'Timed out waiting for Google Maps to render.');
        }
      }, 8000);
    });
  }

  function waitReady(timeoutMs) {
    timeoutMs = timeoutMs || 8000;
    return new Promise(function (resolve) {
      var el = $('gmap');
      if (!gmap || !el) {
        resolve({ ok: false, reason: 'Google map was not created.' });
        return;
      }
      var settled = false;
      function done(ok, reason) {
        if (settled) return;
        settled = true;
        resolve({ ok: ok, reason: reason || (ok ? 'ok' : 'failed') });
      }

      var prevAuth = global.gm_authFailure;
      global.gm_authFailure = function () {
        done(
          false,
          'Google rejected this API key. Enable Maps JavaScript API + billing; Application restrictions = None for desktop.'
        );
        if (typeof prevAuth === 'function') {
          try {
            prevAuth();
          } catch (e) {}
        }
      };

      global.google.maps.event.addListenerOnce(gmap, 'tilesloaded', function () {
        setTimeout(function () {
          if (looksBroken(el)) {
            done(
              false,
              'Google Maps showed “Oops! Something went wrong.” Check billing, Maps JavaScript API, Application restrictions = None.'
            );
          } else {
            done(true, 'ok');
          }
        }, 350);
      });

      setTimeout(function () {
        if (looksBroken(el)) {
          done(
            false,
            'Google Maps showed “Oops! Something went wrong.” Check billing, Maps JavaScript API, Application restrictions = None.'
          );
        } else if (el.querySelector('canvas, img, .gm-style')) {
          done(true, 'ok');
        } else {
          done(false, 'Timed out waiting for Google Maps tiles.');
        }
      }, timeoutMs);
    });
  }

  function createGoogleMap() {
    if (gmap && $('gmap') && looksBroken($('gmap'))) {
      destroyGoogleMap();
    }
    if (gmap) return gmap;
    var host = $('gmap');
    if (!host) throw new Error('Google map container missing.');
    // Full-size under MapLibre — real viewport, not interactive yet.
    setLayer('loading');
    var center = { lat: 20, lng: 0 };
    var zoom = 2;
    if (mlMap) {
      var c = mlMap.getCenter();
      center = { lat: c.lat, lng: c.lng };
      zoom = mlMap.getZoom();
    }
    var minWorldZoom = minimumWorldZoom(host);
    zoom = Math.max(minWorldZoom, Number(zoom) || minWorldZoom);
    gmap = new global.google.maps.Map(host, {
      center: center,
      zoom: zoom,
      minZoom: minWorldZoom,
      restriction: {
        latLngBounds: { north: 85, south: -85, west: -180, east: 180 },
        strictBounds: true
      },
      mapTypeId: mapTypeId,
      mapTypeControl: false, // header owns map type (roadmap/satellite/…)
      streetViewControl: false,
      fullscreenControl: false,
      zoomControl: true,
      zoomControlOptions: {
        position: global.google.maps.ControlPosition.RIGHT_BOTTOM
      },
      gestureHandling: 'greedy',
      clickableIcons: false
    });
    if (typeof clickHandler === 'function') {
      gmap.addListener('click', function (e) {
        if (!e.latLng) return;
        clickHandler(e.latLng.lat(), e.latLng.lng());
      });
    }
    return gmap;
  }

  // A window can move between displays or be resized after Google starts.
  // Keep the one-world rule true without recreating the map or its overlays.
  global.addEventListener('resize', function () {
    if (!gmap) return;
    global.setTimeout(enforceGoogleWorldZoom, 80);
  });

  function syncMapTypeUi() {
    var typeSel = $('gmapsMapTypeSelect');
    var typeWrap = $('gmapsMapTypeWrap');
    var onGoogle = provider === 'google';
    if (typeWrap) {
      if (onGoogle) {
        typeWrap.hidden = false;
        typeWrap.removeAttribute('hidden');
        typeWrap.style.display = 'block';
        typeWrap.setAttribute('aria-hidden', 'false');
      } else {
        typeWrap.hidden = true;
        typeWrap.setAttribute('hidden', '');
        typeWrap.style.display = 'none';
        typeWrap.setAttribute('aria-hidden', 'true');
      }
    }
    if (typeSel) {
      typeSel.disabled = !onGoogle;
      if (MAP_TYPES[mapTypeId]) typeSel.value = mapTypeId;
    }
  }

  function notify() {
    if (typeof onProviderChange === 'function') {
      try {
        onProviderChange(provider);
      } catch (e) {}
    }
    var on = provider === 'google';
    // Google Maps animates a DOM/tile surface.  Expensive glass backdrop
    // filters and an actively painted hidden MapLibre canvas make its zoom
    // noticeably choppy in WebKit/pywebview.  The body class lets CSS switch
    // those effects to solid fallbacks while Google is active.
    if (global.document && global.document.body) {
      global.document.body.classList.toggle('provider-google', on);
      global.document.body.classList.toggle('provider-maplibre', !on);
    }
    var btn = $('gmapsToggleBtn');
    if (btn) {
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.title = on ? 'Switch back to OpenFreeMap' : 'Switch to Google Maps';
    }
    var mlBtn = $('mapProviderMaplibreBtn');
    if (mlBtn) {
      mlBtn.classList.toggle('active', !on);
      mlBtn.setAttribute('aria-pressed', on ? 'false' : 'true');
    }
    var styleSel = $('styleSelect');
    if (styleSel) {
      styleSel.disabled = on;
      styleSel.title = on
        ? 'OpenFreeMap styles apply when using the OpenFreeMap basemap'
        : 'OpenFreeMap basemap colors';
    }
    var viewSeg = $('viewSeg');
    if (viewSeg) {
      viewSeg.style.opacity = on ? '0.45' : '';
      viewSeg.querySelectorAll('button').forEach(function (b) {
        b.disabled = on;
      });
    }
    syncMapTypeUi();
    // Layout hook for chrome / CSS that depends on active basemap.
    var area = $('mapArea');
    if (area) {
      area.classList.toggle('provider-google', on);
      area.classList.toggle('provider-maplibre', !on);
    }
    var header = global.document.querySelector('header');
    if (header) {
      header.classList.toggle('provider-google', on);
      header.classList.toggle('provider-maplibre', !on);
    }
  }

  var MMapsProvider = {
    /**
     * @param {object} opts
     * @param {maplibregl.Map} opts.maplibre
     * @param {function(number,number)} [opts.onMapClick] lat, lon
     * @param {function(string)} [opts.onProviderChange]
     */
    init: function (opts) {
      opts = opts || {};
      mlMap = opts.maplibre || null;
      clickHandler = opts.onMapClick || null;
      onProviderChange = opts.onProviderChange || null;
      provider = 'maplibre';
      setLayer('off');
      notify();
    },

    getProvider: function () {
      return provider;
    },

    getGoogleMap: function () {
      return gmap;
    },

    getMaplibreMap: function () {
      return mlMap;
    },

    /**
     * Google basemap type: roadmap | satellite | hybrid | terrain.
     * Only affects the Google map instance (MapLibre ignores this).
     */
    getMapType: function () {
      return mapTypeId;
    },

    setMapType: function (id) {
      var next = String(id || '')
        .trim()
        .toLowerCase();
      if (!MAP_TYPES[next]) {
        return { ok: false, reason: 'Unknown map type.' };
      }
      mapTypeId = next;
      if (gmap && global.google && global.google.maps) {
        try {
          gmap.setMapTypeId(mapTypeId);
        } catch (e) {
          return {
            ok: false,
            reason: (e && e.message) ? e.message : String(e)
          };
        }
      }
      syncMapTypeUi();
      return { ok: true, mapTypeId: mapTypeId };
    },

    /** Validator for MMapsKeys.test — Maps JS only (no Geocoding required). */
    validateKey: function (key) {
      return loadScript(key).then(function () {
        return probeRender();
      });
    },

    switchToGoogle: function (key) {
      if (switching) return Promise.resolve({ ok: false, reason: 'Switch already in progress.' });
      key = (key || '').trim();
      if (!key) {
        return Promise.resolve({
          ok: false,
          reason: 'Add a Google Maps API key in Settings first.'
        });
      }
      switching = true;
      return loadScript(key)
        .then(function () {
          createGoogleMap();
          try {
            global.google.maps.event.trigger(gmap, 'resize');
          } catch (e) {}
          if (mlMap) {
            var c = mlMap.getCenter();
            gmap.setCenter({ lat: c.lat, lng: c.lng });
            gmap.setZoom(mlMap.getZoom());
          }
          return waitReady(8000);
        })
        .then(function (ready) {
          if (!ready.ok) {
            destroyGoogleMap();
            provider = 'maplibre';
            notify();
            return ready;
          }
          setLayer('on');
          try {
            global.google.maps.event.trigger(gmap, 'resize');
          } catch (e) {}
          if (mlMap) {
            var c2 = mlMap.getCenter();
            gmap.setCenter({ lat: c2.lat, lng: c2.lng });
            gmap.setZoom(mlMap.getZoom());
          }
          provider = 'google';
          notify();
          return { ok: true, reason: 'ok' };
        })
        .catch(function (err) {
          destroyGoogleMap();
          provider = 'maplibre';
          notify();
          return {
            ok: false,
            reason: (err && err.message) ? err.message : String(err),
            code: (err && err.mmapsCode) || null
          };
        })
        .then(function (result) {
          switching = false;
          return result;
        });
    },

    switchToMapLibre: function () {
      if (provider === 'google' && gmap && mlMap) {
        try {
          var c = gmap.getCenter();
          if (c) mlMap.jumpTo({ center: [c.lng(), c.lat()], zoom: gmap.getZoom() });
        } catch (e) {}
      }
      provider = 'maplibre';
      if ($('gmap') && looksBroken($('gmap'))) {
        destroyGoogleMap();
      } else {
        setLayer('off');
      }
      try {
        if (mlMap) mlMap.resize();
      } catch (e) {}
      notify();
      return { ok: true };
    },

    toggle: function (key) {
      if (provider === 'google') {
        MMapsProvider.switchToMapLibre();
        return Promise.resolve({ ok: true, provider: 'maplibre' });
      }
      return MMapsProvider.switchToGoogle(key).then(function (r) {
        r.provider = provider;
        return r;
      });
    },

    /** After style reload on MapLibre, caller may need resize. */
    notifyMaplibreResized: function () {
      try {
        if (mlMap) mlMap.resize();
      } catch (e) {}
    }
  };

  global.MMapsProvider = MMapsProvider;
})(typeof window !== 'undefined' ? window : globalThis);
