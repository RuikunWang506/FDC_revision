/* Shared by live V7 List Creator pages and cached HTML snapshots. */
(function () {
    function $(id) {
        return document.getElementById(id);
    }

    window.syncRemoveStylesQtyControls = function syncRemoveStylesQtyControls() {
        var qtyBox = $('removeStylesByQty') || $('removeStylesByTotalQty');
        var enabled = !!(qtyBox && qtyBox.checked);
        ['removeStylesColumnSelect', 'removeStylesComparisonSelect', 'removeStylesQtyThreshold'].forEach(function (id) {
            if ($(id)) $(id).disabled = !enabled;
        });
    };

    function styleMeetsComparison(value, comparison, threshold) {
        if (comparison === 'lte') return value <= threshold;
        if (comparison === 'eq') return value === threshold;
        if (comparison === 'gt') return value > threshold;
        // "Less than 0" means no stock (0 or below). Other thresholds stay strict <.
        if (comparison === 'lt' && threshold === 0) return value <= 0;
        return value < threshold;
    }

    function isStyleNotInOms(card) {
        var title = card.querySelector('.card-title') || card;
        var text = (title.textContent || '').toUpperCase().replace(/\s+/g, '_');
        if (text.indexOf('NOT_IN_OMS') !== -1) return true;
        if (title.querySelector && title.querySelector('.badge-warning')) return true;
        return card.getAttribute('data-in-oms') === '0';
    }

    function getStyleColumnSum(card, column) {
        var selector = '.total-cell';
        if (column === 'quantity') selector = '.quantity-cell';
        else if (column === 'ocean') selector = '.ocean-cell';
        var total = 0;
        card.querySelectorAll('tbody tr ' + selector).forEach(function (cell) {
            total += parseInt((cell.innerText || '').replace(/,/g, ''), 10) || 0;
        });
        return total;
    }

    function getStyleProductCards() {
        var cards = Array.from(document.querySelectorAll('[data-style-card="1"], .style-product-card'));
        if (!cards.length) {
            cards = Array.from(document.querySelectorAll('.card.mb-3, .card')).filter(function (card) {
                return card.querySelector('.card-title') && card.querySelector('table tbody tr');
            });
        }
        return cards;
    }

    function dropStyleCard(card) {
        if (!card) return;
        try {
            if (typeof window.removeStyleCard === 'function') {
                window.removeStyleCard(card);
                if (document.body.contains(card)) card.remove();
                return;
            }
        } catch (err) { /* fall through */ }
        card.remove();
        if (typeof window.updateStyleCardCount === 'function') window.updateStyleCardCount();
    }

    window.removeStylesWhere = function removeStylesWhere() {
        var notInOms = !!($('removeStylesNotInOms') && $('removeStylesNotInOms').checked);
        var qtyBox = $('removeStylesByQty') || $('removeStylesByTotalQty');
        var byQty = !!(qtyBox && qtyBox.checked);
        if (!notInOms && !byQty) {
            alert('Select at least one condition (Not in OMS and/or Quantity / Ocean / Total).');
            return;
        }
        var column = 'quantity';
        var comparison = 'lt';
        var threshold = 0;
        if (byQty) {
            column = ($('removeStylesColumnSelect') && $('removeStylesColumnSelect').value) || 'quantity';
            comparison = ($('removeStylesComparisonSelect') && $('removeStylesComparisonSelect').value) || 'lt';
            threshold = parseInt($('removeStylesQtyThreshold') && $('removeStylesQtyThreshold').value, 10);
            if (isNaN(threshold)) {
                alert('Please enter a valid number.');
                return;
            }
        }
        var removed = 0;
        getStyleProductCards().forEach(function (card) {
            var cardIsNotInOms = isStyleNotInOms(card);
            // NOT_IN_OMS cards contain zero-value placeholder rows. Do not let a
            // quantity filter remove them unless that condition is selected.
            if (cardIsNotInOms && !notInOms) return;
            if (notInOms && !cardIsNotInOms) return;
            if (byQty && !styleMeetsComparison(getStyleColumnSum(card, column), comparison, threshold)) return;
            dropStyleCard(card);
            removed += 1;
        });
        if (typeof window.updateRowCounts === 'function') window.updateRowCounts();
        if (typeof window.updateCurrentStyleList === 'function') window.updateCurrentStyleList();
        if (removed === 0) {
            alert('No styles matched both checked conditions.');
        }
    };

    window.updateCurrentStyleList = function updateCurrentStyleList() {
        var box = $('lcCurrentStyleListLines');
        var countEl = $('lcCurrentStyleCount');
        if (!box) return;
        var styles = [];
        getStyleProductCards().forEach(function (card) {
            var name = '';
            if (typeof window.getCardStyle === 'function') name = window.getCardStyle(card) || '';
            if (!name) {
                var title = card.querySelector('.card-title');
                if (title) name = (title.firstChild ? title.firstChild.textContent : title.textContent || '').trim();
            }
            if (name) styles.push(name);
        });
        if (!styles.length) {
            box.innerHTML = '<div class="text-muted">No styles</div>';
        } else {
            box.innerHTML = styles.map(function (s) {
                return '<div class="lc-your-input-row" translate="no">' +
                    String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') +
                    '</div>';
            }).join('');
        }
        if (countEl) countEl.textContent = '(' + styles.length + ')';
    };

    var IMAGE_EXT = { jpg: 1, jpeg: 1, png: 1, gif: 1, bmp: 1, tiff: 1, tif: 1, webp: 1 };
    var MISSING_IMAGE_CSS = [
        '.lc-missing-image-upload{width:100%}',
        '.lc-missing-image-dropzone{width:100%;min-height:180px;border:2px dashed #94a3b8;border-radius:12px;background:#f8fafc;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:1.25rem 1rem;color:#64748b;cursor:pointer}',
        '.lc-missing-image-dropzone.lc-dragover{border-color:#4f46e5;background:#eef2ff;color:#3730a3}',
        '.lc-missing-image-dropzone.is-busy{pointer-events:none;opacity:.75;cursor:wait}',
        '.lc-missing-image-title{font-weight:600;color:#334155;margin-bottom:.2rem}',
        '.lc-missing-image-hint{font-size:.9rem;margin-bottom:.75rem}',
        '.lc-missing-image-status{font-size:.85rem;margin-top:.65rem}',
        '.lc-missing-image-status.is-error{color:#b91c1c}',
        '.lc-missing-image-status.is-ok{color:#166534}'
    ].join('');

    function ensureMissingImageCss() {
        if (document.getElementById('lc-missing-image-upload-css')) return;
        var style = document.createElement('style');
        style.id = 'lc-missing-image-upload-css';
        style.textContent = MISSING_IMAGE_CSS;
        document.head.appendChild(style);
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function closestEl(node, selector) {
        if (!node) return null;
        if (node.nodeType === 3) node = node.parentElement;
        return (node && node.closest) ? node.closest(selector) : null;
    }

    function cardStyleName(card) {
        if (!card) return '';
        var fromData = (card.getAttribute('data-style') || '').trim();
        if (fromData) return fromData;
        if (typeof window.getCardStyle === 'function') {
            var named = window.getCardStyle(card);
            if (named) return String(named).trim();
        }
        var title = card.querySelector('.card-title');
        if (!title) return '';
        return (title.firstChild ? title.firstChild.textContent : title.textContent || '').trim();
    }

    function getCardImageGallery(card) {
        if (!card) return null;
        var wrap = card.querySelector('.lc-card-images');
        if (wrap) return wrap;
        var cols = card.querySelectorAll('.row > .col-6');
        if (cols.length >= 2) {
            return cols[1].querySelector('.d-flex.flex-wrap') || cols[1];
        }
        return card.querySelector('.d-flex.flex-wrap');
    }

    function lookupImagesForStyle(map, style) {
        if (!map || !style) return null;
        if (map[style] && map[style].length) return map[style];
        var upper = String(style).toUpperCase();
        if (map[upper] && map[upper].length) return map[upper];
        var keys = Object.keys(map);
        for (var i = 0; i < keys.length; i++) {
            if (String(keys[i]).toUpperCase() === upper && map[keys[i]] && map[keys[i]].length) {
                return map[keys[i]];
            }
        }
        return null;
    }

    function setUploadStatus(widget, message, kind) {
        if (!widget) return;
        var status = widget.querySelector('.lc-missing-image-status');
        var zone = widget.querySelector('.lc-missing-image-dropzone');
        if (!status) return;
        if (!message) {
            status.hidden = true;
            status.textContent = '';
            status.className = 'lc-missing-image-status';
            return;
        }
        status.hidden = false;
        status.textContent = message;
        status.className = 'lc-missing-image-status' + (kind ? ' is-' + kind : '');
        if (zone && kind === 'error') zone.classList.remove('is-busy');
    }

    function missingImageWidgetHtml(style) {
        return '<div class="lc-missing-image-upload" data-style="' + escapeHtml(style) + '">' +
            '<div class="lc-missing-image-dropzone" tabindex="0">' +
            '<input type="file" accept="image/*" class="lc-missing-image-input" hidden>' +
            '<div class="lc-missing-image-title">No catalog image</div>' +
            '<div class="lc-missing-image-hint">Drop a photo here, or upload one for this style.</div>' +
            '<button type="button" class="btn btn-sm btn-primary lc-missing-image-browse">Upload picture</button>' +
            '<div class="lc-missing-image-status" hidden></div>' +
            '</div></div>';
    }

    function ensureMissingImageWidget(card, style) {
        var gallery = getCardImageGallery(card);
        if (!gallery || !style) return null;
        gallery.classList.add('lc-card-images');
        var existing = gallery.querySelector('.lc-missing-image-upload');
        if (existing) return existing;
        gallery.insertAdjacentHTML('beforeend', missingImageWidgetHtml(style));
        return gallery.querySelector('.lc-missing-image-upload');
    }

    function renderCardCatalogImages(card, style, images) {
        var gallery = getCardImageGallery(card);
        if (!gallery || !images || !images.length) return;
        gallery.classList.add('lc-card-images');
        var stamp = Date.now();
        var html = images.map(function (image, idx) {
            var id = 'catalog_image_' + String(style).replace(/\s+/g, '_') + '_' + (idx + 1) + '_' + stamp;
            var thumb = image.path || image.fullsize_path || '';
            var full = image.fullsize_path || image.path || '';
            var checked = idx === 0 ? ' checked' : '';
            return '<div class="image-option">' +
                '<input type="radio" id="' + escapeHtml(id) + '" name="image_option_' + escapeHtml(style) + '" value="' + escapeHtml(full) + '"' + checked + '>' +
                '<label for="' + escapeHtml(id) + '">' +
                '<img src="' + escapeHtml(thumb) + '" data-fullsize="' + escapeHtml(full) + '" alt="' + escapeHtml(style) + '" class="img-thumbnail">' +
                '</label></div>';
        }).join('');
        gallery.innerHTML = html;
        gallery.querySelectorAll('img.img-thumbnail').forEach(function (img) {
            img.addEventListener('click', function () {
                if (typeof window.enlargeImage === 'function') {
                    window.enlargeImage(img, img.dataset.fullsize);
                }
            });
        });
    }

    function jsonFromResponse(response) {
        return response.text().then(function (text) {
            if (!text) return {};
            try {
                return JSON.parse(text);
            } catch (err) {
                return { success: false, message: 'Unexpected response from server' };
            }
        });
    }

    function fetchStyleCatalogImages(styles) {
        var unique = [];
        var seen = {};
        (styles || []).forEach(function (style) {
            var s = String(style || '').trim();
            if (!s) return;
            var key = s.toUpperCase();
            if (seen[key]) return;
            seen[key] = 1;
            unique.push(s);
        });
        if (!unique.length) return Promise.resolve({});
        return fetch('/v7_listcreator/style_catalog_images', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ styles: unique })
        }).then(function (response) {
            return jsonFromResponse(response).then(function (data) {
                if (!response.ok || !data || !data.success) return {};
                return data.images || {};
            });
        }).catch(function () {
            return {};
        });
    }

    function imagesFromProcessAssets(assets, style) {
        if (!assets || !assets.length) return [];
        var upper = String(style || '').toUpperCase();
        var matched = assets.filter(function (asset) {
            return String((asset && asset.sku) || '').toUpperCase() === upper;
        });
        var use = matched.length ? matched : assets;
        return use.filter(function (asset) {
            return asset && (asset.path || asset.fullsize_path);
        }).map(function (asset) {
            return {
                path: asset.path || asset.fullsize_path,
                fullsize_path: asset.fullsize_path || asset.path,
                filename: asset.filename || '',
                asset_id: asset.asset_id
            };
        });
    }

    function fileNamedForStyle(file, style, index) {
        var original = file && file.name ? String(file.name) : 'photo.jpg';
        var ext = (original.split('.').pop() || 'jpg').toLowerCase();
        if (!IMAGE_EXT[ext]) ext = 'jpg';
        var safe = String(style || 'STYLE').replace(/[^\w\-]+/g, '_');
        var name = index ? (safe + '_' + (index + 1) + '.' + ext) : (safe + '.' + ext);
        try {
            return new File([file], name, { type: file.type || 'image/jpeg', lastModified: file.lastModified });
        } catch (err) {
            return file;
        }
    }

    function imageFilesFromList(fileList) {
        var out = [];
        Array.prototype.forEach.call(fileList || [], function (file) {
            if (!file) return;
            var ext = ((file.name || '').split('.').pop() || '').toLowerCase();
            if (file.type && file.type.indexOf('image/') === 0) out.push(file);
            else if (IMAGE_EXT[ext]) out.push(file);
        });
        return out;
    }

    function sleep(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    function refreshCardAfterUpload(card, style, immediateImages) {
        if (immediateImages && immediateImages.length) {
            renderCardCatalogImages(card, style, immediateImages);
            return Promise.resolve(true);
        }
        var attempt = 0;
        function next() {
            attempt += 1;
            return fetchStyleCatalogImages([style]).then(function (map) {
                var images = lookupImagesForStyle(map, style);
                if (images && images.length) {
                    renderCardCatalogImages(card, style, images);
                    return true;
                }
                if (attempt >= 5) return false;
                return sleep(400).then(next);
            });
        }
        return next();
    }

    function uploadCatalogFilesForCard(card, files) {
        var style = cardStyleName(card);
        var widget = card.querySelector('.lc-missing-image-upload');
        var zone = widget && widget.querySelector('.lc-missing-image-dropzone');
        if (!style) {
            setUploadStatus(widget, 'Could not determine this style.', 'error');
            return Promise.resolve();
        }
        var imageFiles = imageFilesFromList(files);
        if (!imageFiles.length) {
            setUploadStatus(widget, 'Please drop a JPG or PNG photo.', 'error');
            return Promise.resolve();
        }
        if (zone) zone.classList.add('is-busy');
        setUploadStatus(widget, 'Uploading and renaming as ' + style + '.jpg…', '');
        var formData = new FormData();
        imageFiles.forEach(function (file, idx) {
            var named = fileNamedForStyle(file, style, idx);
            formData.append('catalog_files', named, named.name || (style + '.jpg'));
        });
        return fetch('/upload_bulk_catalog', { method: 'POST', body: formData })
            .then(function (response) { return jsonFromResponse(response).then(function (data) { return { response: response, data: data }; }); })
            .then(function (upload) {
                var data = upload.data || {};
                if (!upload.response.ok || !data.success) {
                    throw new Error(data.message || 'Upload failed');
                }
                if ((!data.images || !data.images.length) && data.duplicates && data.duplicates.length) {
                    setUploadStatus(widget, 'This photo is already in the catalog. Checking this style…', '');
                    return refreshCardAfterUpload(card, style, []).then(function (ok) {
                        if (ok) return;
                        var dupSku = (data.duplicates[0].duplicate_of && data.duplicates[0].duplicate_of.sku) || data.duplicates[0].sku || '';
                        throw new Error('This photo already exists in the catalog' + (dupSku ? ' as ' + dupSku : '') + '.');
                    });
                }
                if (!data.session_id || !data.images || !data.images.length) {
                    throw new Error(data.message || 'No new catalog image to save');
                }
                var payload = {
                    session_id: data.session_id,
                    images: data.images.map(function (img) {
                        return { path: img.path, sku: style };
                    })
                };
                setUploadStatus(widget, 'Saving as ' + style + '.jpg…', '');
                return fetch('/process_bulk_catalog', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }).then(function (response) {
                    return jsonFromResponse(response).then(function (processed) {
                        if (!response.ok || !processed.success) {
                            throw new Error(processed.message || 'Could not save catalog image');
                        }
                        var immediate = imagesFromProcessAssets(processed.assets, style);
                        return refreshCardAfterUpload(card, style, immediate).then(function (ok) {
                            if (!ok) {
                                throw new Error('Saved, but the card image is not ready yet. Reload this page in a moment.');
                            }
                        });
                    });
                });
            })
            .catch(function (err) {
                setUploadStatus(widget, (err && err.message) ? err.message : 'Upload failed', 'error');
                if (zone) zone.classList.remove('is-busy');
            });
    }

    function bindMissingImageUploads() {
        if (document.documentElement.getAttribute('data-lc-missing-image-bound') === '1') return;
        document.documentElement.setAttribute('data-lc-missing-image-bound', '1');

        document.addEventListener('click', function (ev) {
            var zone = closestEl(ev.target, '.lc-missing-image-dropzone');
            if (!zone || zone.classList.contains('is-busy')) return;
            if (closestEl(ev.target, '.lc-missing-image-input')) return;
            ev.preventDefault();
            var input = zone.querySelector('.lc-missing-image-input');
            if (input) input.click();
        });

        document.addEventListener('change', function (ev) {
            var input = ev.target;
            if (!input || !input.classList || !input.classList.contains('lc-missing-image-input')) return;
            var card = input.closest('.style-product-card') || input.closest('.card');
            var files = input.files;
            input.value = '';
            if (card && files && files.length) uploadCatalogFilesForCard(card, files);
        });

        document.addEventListener('dragover', function (ev) {
            var zone = closestEl(ev.target, '.lc-missing-image-dropzone');
            if (!zone) return;
            ev.preventDefault();
            zone.classList.add('lc-dragover');
        });

        document.addEventListener('dragleave', function (ev) {
            var zone = closestEl(ev.target, '.lc-missing-image-dropzone');
            if (!zone) return;
            zone.classList.remove('lc-dragover');
        });

        document.addEventListener('drop', function (ev) {
            var zone = closestEl(ev.target, '.lc-missing-image-dropzone');
            if (!zone) return;
            ev.preventDefault();
            zone.classList.remove('lc-dragover');
            if (zone.classList.contains('is-busy')) return;
            var card = closestEl(zone, '.style-product-card') || closestEl(zone, '.card');
            if (card && ev.dataTransfer && ev.dataTransfer.files) {
                uploadCatalogFilesForCard(card, ev.dataTransfer.files);
            }
        });
    }

    function bootMissingImageUploads() {
        ensureMissingImageCss();
        bindMissingImageUploads();
        var emptyCards = [];
        getStyleProductCards().forEach(function (card) {
            var style = cardStyleName(card);
            if (style && !card.getAttribute('data-style')) card.setAttribute('data-style', style);
            var gallery = getCardImageGallery(card);
            if (!gallery) return;
            gallery.classList.add('lc-card-images');
            if (gallery.querySelector('.image-option')) return;
            emptyCards.push({ card: card, style: style });
        });
        if (!emptyCards.length) return;
        emptyCards.forEach(function (item) {
            ensureMissingImageWidget(item.card, item.style);
        });
        fetchStyleCatalogImages(emptyCards.map(function (item) { return item.style; })).then(function (map) {
            emptyCards.forEach(function (item) {
                var images = lookupImagesForStyle(map, item.style);
                if (images && images.length) {
                    renderCardCatalogImages(item.card, item.style, images);
                }
            });
        });
    }

    function bootStyleTools() {
        window.syncRemoveStylesQtyControls();
        window.updateCurrentStyleList();
        bootMissingImageUploads();
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootStyleTools);
    } else {
        bootStyleTools();
    }
})();
