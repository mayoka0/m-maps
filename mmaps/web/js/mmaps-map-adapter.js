/**
 * MMapsAdapter - Stream 4: single overlay API for features.
 *
 * Features (IP approx, routes, trips, status poll) call ONLY this module
 * for map visuals. They must not touch google.maps or dual-write helpers.
 *
 * Stores one overlay model and mirrors it to both providers.  Updates are
 * deliberately cheap because /status runs every second while Google may be
 * animating a zoom.
 */
(function (global) {
  'use strict';

  var mlMap = null;
  var maplibregl = null;
  var lines = {};
  var dotMarker = null;
  var destMarker = null;
  var approxMarker = null;
  var dotStale = false;
  var gDot = null;
  var gDotStale = null;
  var gDest = null;
  var gApprox = null;
  var gPolylines = {};
  var gTripMarkers = [];
  var lastTripSpecs = [];
  var lastDot = null;
  var lastDest = null;
  var lastApprox = null;
  var gDotPosition = null;
  var gDestPosition = null;
  var gApproxPosition = null;

  function gmap() {
    return global.MMapsProvider && global.MMapsProvider.getGoogleMap
      ? global.MMapsProvider.getGoogleMap()
      : null;
  }

  function hasGoogle() {
    return !!(global.google && global.google.maps && gmap());
  }

  function samePosition(last, lat, lon) {
    return !!last && last.lat === lat && last.lon === lon;
  }

  function renderMlLine(id) {
    if (!mlMap || !lines[id]) return;
    var l = lines[id];
    var data = {
      type: 'Feature',
      geometry: { type: 'LineString', coordinates: l.coordinates }
    };
    if (mlMap.getSource(id)) {
      mlMap.getSource(id).setData(data);
      return;
    }
    mlMap.addSource(id, { type: 'geojson', data: data });
    mlMap.addLayer({
      id: id,
      type: 'line',
      source: id,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': l.color,
        'line-width': 4,
        'line-opacity': 0.85
      }
    });
  }

  function renderGLine(id) {
    if (!hasGoogle()) return;
    var l = lines[id];
    var gm = gmap();
    if (!l || !l.coordinates || l.coordinates.length < 2) {
      if (gPolylines[id]) {
        gPolylines[id].setMap(null);
        delete gPolylines[id];
      }
      return;
    }
    var path = l.coordinates.map(function (c) {
      return { lat: c[1], lng: c[0] };
    });
    // Road/trip coordinates are already dense polylines.  Marking every short
    // segment geodesic makes Google reproject a large path on each zoom frame.
    // Flights alone need great-circle rendering.
    var geodesic = id === 'flight';
    if (gPolylines[id]) {
      gPolylines[id].setPath(path);
      gPolylines[id].setOptions({ strokeColor: l.color, geodesic: geodesic });
      if (gPolylines[id].getMap() !== gm) gPolylines[id].setMap(gm);
      return;
    }
    gPolylines[id] = new global.google.maps.Polyline({
      path: path,
      geodesic: geodesic,
      strokeColor: l.color || '#3b82f6',
      strokeOpacity: 0.85,
      strokeWeight: 4,
      map: gm
    });
  }

  function setGDot(lat, lon, stale) {
    if (!hasGoogle()) return;
    var gm = gmap();
    var isStale = !!stale;
    var color = isStale ? '#f59e0b' : '#3b82f6';
    var icon = {
      path: global.google.maps.SymbolPath.CIRCLE,
      scale: 8,
      fillColor: color,
      fillOpacity: 1,
      strokeColor: '#ffffff',
      strokeWeight: 2
    };
    if (gDot) {
      if (!samePosition(gDotPosition, lat, lon)) {
        gDot.setPosition({ lat: lat, lng: lon });
        gDotPosition = { lat: lat, lon: lon };
      }
      // Re-attach only when required. setMap() on every status poll forces
      // unnecessary overlay work and can interrupt an in-progress zoom.
      if (gDot.getMap() !== gm) gDot.setMap(gm);
      if (gDotStale !== isStale) {
        gDot.setIcon(icon);
        gDotStale = isStale;
      }
      return;
    }
    gDotStale = isStale;
    gDotPosition = { lat: lat, lon: lon };
    gDot = new global.google.maps.Marker({
      position: { lat: lat, lng: lon },
      map: gm,
      icon: icon
    });
  }

  function setGDest(lat, lon) {
    if (!hasGoogle()) return;
    var gm = gmap();
    if (gDest) {
      if (!samePosition(gDestPosition, lat, lon)) {
        gDest.setPosition({ lat: lat, lng: lon });
        gDestPosition = { lat: lat, lon: lon };
      }
      if (gDest.getMap() !== gm) gDest.setMap(gm);
      return;
    }
    gDestPosition = { lat: lat, lon: lon };
    gDest = new global.google.maps.Marker({
      position: { lat: lat, lng: lon },
      map: gm,
      icon: {
        path: global.google.maps.SymbolPath.CIRCLE,
        scale: 8,
        fillColor: '#f59e0b',
        fillOpacity: 1,
        strokeColor: '#ffffff',
        strokeWeight: 2
      }
    });
  }

  function setGApprox(lat, lon) {
    if (!hasGoogle()) return;
    var gm = gmap();
    if (gApprox) {
      if (!samePosition(gApproxPosition, lat, lon)) {
        gApprox.setPosition({ lat: lat, lng: lon });
        gApproxPosition = { lat: lat, lon: lon };
      }
      if (gApprox.getMap() !== gm) gApprox.setMap(gm);
      return;
    }
    gApproxPosition = { lat: lat, lon: lon };
    gApprox = new global.google.maps.Marker({
      position: { lat: lat, lng: lon },
      map: gm,
      icon: {
        path: global.google.maps.SymbolPath.CIRCLE,
        scale: 8,
        fillColor: '#94a3b8',
        fillOpacity: 1,
        strokeColor: '#ffffff',
        strokeWeight: 2
      }
    });
  }

  function clearGTrip() {
    gTripMarkers.forEach(function (m) {
      try {
        m.setMap(null);
      } catch (e) {}
    });
    gTripMarkers = [];
  }

  function addGTrip(lat, lon, text, title, isAirport) {
    if (!hasGoogle()) return;
    var gm = gmap();
    var marker = new global.google.maps.Marker({
      position: { lat: lat, lng: lon },
      map: gm,
      label: {
        text: String(text),
        color: '#ffffff',
        fontWeight: '700',
        fontSize: '11px'
      },
      title: title || '',
      icon: {
        path: global.google.maps.SymbolPath.CIRCLE,
        scale: 12,
        fillColor: isAirport ? '#0d9488' : '#6366f1',
        fillOpacity: 1,
        strokeColor: '#ffffff',
        strokeWeight: 2
      }
    });
    gTripMarkers.push(marker);
  }

  function replayGoogleOverlays() {
    if (!hasGoogle()) return;
    if (lastDot) setGDot(lastDot.lat, lastDot.lon, lastDot.stale);
    if (lastDest) setGDest(lastDest.lat, lastDest.lon);
    if (lastApprox) setGApprox(lastApprox.lat, lastApprox.lon);
    Object.keys(lines).forEach(function (id) {
      renderGLine(id);
    });
    clearGTrip();
    lastTripSpecs.forEach(function (s) {
      addGTrip(s.lat, s.lon, s.text, s.title, s.airport);
    });
  }

  var MMapsAdapter = {
    /**
     * @param {object} opts
     * @param {maplibregl.Map} opts.maplibre
     * @param {object} opts.maplibregl library
     */
    init: function (opts) {
      opts = opts || {};
      mlMap = opts.maplibre;
      maplibregl = opts.maplibregl;
      lines = {};
    },

    /** Re-attach MapLibre line layers after setStyle. */
    onMaplibreStyleLoad: function () {
      Object.keys(lines).forEach(renderMlLine);
    },

    /** Call after provider switches to Google so overlays appear. */
    onProviderChanged: function (provider) {
      if (provider === 'google') replayGoogleOverlays();
    },

    setLine: function (id, coordinates, color) {
      lines[id] = { coordinates: coordinates, color: color };
      renderMlLine(id);
      renderGLine(id);
    },

    removeLine: function (id) {
      delete lines[id];
      if (mlMap) {
        if (mlMap.getLayer(id)) mlMap.removeLayer(id);
        if (mlMap.getSource(id)) mlMap.removeSource(id);
      }
      if (gPolylines[id]) {
        gPolylines[id].setMap(null);
        delete gPolylines[id];
      }
    },

    showDot: function (lat, lon, opts) {
      opts = opts || {};
      var stale = !!opts.stale;
      lastDot = { lat: lat, lon: lon, stale: stale };
      var color = stale ? '#f59e0b' : '#3b82f6';
      if (dotMarker && dotStale === stale) {
        dotMarker.setLngLat([lon, lat]);
      } else if (mlMap && maplibregl) {
        if (dotMarker) {
          dotMarker.remove();
          dotMarker = null;
        }
        dotStale = stale;
        dotMarker = new maplibregl.Marker({ color: color })
          .setLngLat([lon, lat])
          .addTo(mlMap);
      }
      setGDot(lat, lon, stale);
    },

    clearDot: function () {
      if (dotMarker) {
        dotMarker.remove();
        dotMarker = null;
      }
      dotStale = false;
      lastDot = null;
      if (gDot) {
        gDot.setMap(null);
        gDot = null;
      }
      gDotStale = null;
      gDotPosition = null;
    },

    showDest: function (lat, lon) {
      lastDest = { lat: lat, lon: lon };
      if (destMarker) destMarker.setLngLat([lon, lat]);
      else if (mlMap && maplibregl) {
        destMarker = new maplibregl.Marker({ color: '#f59e0b' })
          .setLngLat([lon, lat])
          .addTo(mlMap);
      }
      setGDest(lat, lon);
    },

    clearDest: function () {
      if (destMarker) {
        destMarker.remove();
        destMarker = null;
      }
      lastDest = null;
      if (gDest) {
        gDest.setMap(null);
        gDest = null;
      }
      gDestPosition = null;
    },

    showApprox: function (lat, lon) {
      lastApprox = { lat: lat, lon: lon };
      if (approxMarker) approxMarker.setLngLat([lon, lat]);
      else if (mlMap && maplibregl) {
        approxMarker = new maplibregl.Marker({ color: '#94a3b8' })
          .setLngLat([lon, lat])
          .addTo(mlMap);
      }
      setGApprox(lat, lon);
    },

    clearApprox: function () {
      if (approxMarker) {
        approxMarker.remove();
        approxMarker = null;
      }
      lastApprox = null;
      if (gApprox) {
        gApprox.setMap(null);
        gApprox = null;
      }
      gApproxPosition = null;
    },

    clearTripMarkers: function () {
      // MapLibre trip markers are owned by the app (custom elements); this only clears Google copies + specs.
      clearGTrip();
      lastTripSpecs = [];
    },

    /**
     * Record a trip stop marker for Google replay. MapLibre marker still created by app.
     */
    pushTripMarkerSpec: function (lat, lon, text, title, isAirport) {
      lastTripSpecs.push({
        lat: lat,
        lon: lon,
        text: String(text),
        title: title || '',
        airport: !!isAirport
      });
      addGTrip(lat, lon, text, title, isAirport);
    },

    flyTo: function (lon, lat, zoom) {
      var googleActive = global.MMapsProvider &&
        global.MMapsProvider.getProvider &&
        global.MMapsProvider.getProvider() === 'google';
      // Animate only the visible provider.  Animating hidden MapLibre while
      // Google pans/zooms wastes a second render loop; switchToMapLibre copies
      // Google's camera back before revealing MapLibre.
      if (!googleActive && mlMap) {
        try {
          var z = zoom != null ? zoom : Math.max(mlMap.getZoom(), 10);
          mlMap.flyTo({ center: [lon, lat], zoom: z, essential: true });
        } catch (e) {}
      }
      var gm = gmap();
      if (googleActive && gm) {
        var z2 = zoom != null ? zoom : gm.getZoom();
        gm.panTo({ lat: lat, lng: lon });
        gm.setZoom(z2);
      }
    }
  };

  global.MMapsAdapter = MMapsAdapter;
})(typeof window !== 'undefined' ? window : globalThis);
