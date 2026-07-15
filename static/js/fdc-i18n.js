/**
 * Client-side i18n helpers — server injects window.FDC_LOCALE and optional window.FDC_I18N map.
 */
(function (global) {
    'use strict';

    var locale = (global.FDC_LOCALE || document.documentElement.lang || 'en').toLowerCase();
    if (locale.indexOf('zh') === 0) locale = 'zh';
    else locale = 'en';

    var catalog = global.FDC_I18N || {};

    function t(key, fallback) {
        var entry = catalog[key];
        if (entry && entry[locale]) return entry[locale];
        if (entry && entry.en) return entry.en;
        return fallback != null ? fallback : key;
    }

    function te(english, fallback) {
        var key = catalog._enIndex && catalog._enIndex[english];
        if (key) return t(key, fallback || english);
        return fallback != null ? fallback : english;
    }

    global.FDC_I18N_API = { locale: locale, t: t, te: te };
})(typeof window !== 'undefined' ? window : this);
