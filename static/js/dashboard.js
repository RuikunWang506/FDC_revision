    let currentFilter = 'all';
    let userFavorites = Array.isArray(DASHBOARD_FAVORITES) ? DASHBOARD_FAVORITES.slice() : [];
    
    function loadFavorites() {
      if (!PORTAL_MINIMAL) {
        userFavorites = Array.isArray(DASHBOARD_FAVORITES) ? DASHBOARD_FAVORITES.slice() : [];
        updateFavorites();
        return Promise.resolve(userFavorites);
      }
      return fetch('/get_favorites')
        .then(response => response.json())
        .then(favorites => {
          userFavorites = favorites;
          updateFavorites();
          return favorites;
        })
        .catch(() => {
          // Fallback to localStorage if server fails
          const fav = localStorage.getItem('userFavorites');
          if (fav) userFavorites = JSON.parse(fav);
          updateFavorites();
          return userFavorites;
        });
    }
    
    function saveFavorites() {
      fetch('/save_favorites', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({favorites: userFavorites})
      })
        .then(response => response.json())
        .then(data => {
          if (!data.success) {
            // Fallback to localStorage
            localStorage.setItem('userFavorites', JSON.stringify(userFavorites));
          }
        })
        .catch(() => {
          // Fallback to localStorage
          localStorage.setItem('userFavorites', JSON.stringify(userFavorites));
        });
    }
    
    function toggleFavorite(event, tool) {
      event.preventDefault();
      event.stopPropagation();
      const index = userFavorites.indexOf(tool);
      if (index > -1) {
        userFavorites.splice(index, 1);
      } else {
        userFavorites.push(tool);
      }
      saveFavorites();
      updateFavorites();
      if (currentFilter === 'favorites' && !userFavorites.length) {
        const allNav = document.querySelector('.dashboard-wrapper .nav-item[onclick*="\'all\'"]');
        filterCategory(allNav, 'all');
        return;
      }
      applyDashboardFilters({ animate: false });
    }
    
    function updateFavorites() {
      if (PORTAL_MINIMAL) return;
      document.querySelectorAll('.favorite-star').forEach(star => {
        const card = star.closest('.tool-card');
        const tool = card ? card.getAttribute('data-tool') : null;
        if (userFavorites.includes(tool)) {
          star.classList.add('favorited');
          star.classList.remove('far');
          star.classList.add('fas');
        } else {
          star.classList.remove('favorited');
          star.classList.remove('fas');
          star.classList.add('far');
        }
      });
      // Only count favorites that are actually present in the DOM
      const visibleFavorites = Array.from(document.querySelectorAll('.tool-card'))
        .filter(card => userFavorites.includes(card.getAttribute('data-tool')))
        .length;
      const favCountEl = document.getElementById('favoritesCount');
      if (favCountEl) favCountEl.textContent = visibleFavorites;
    }
    
    function getDashboardSearchTerm() {
      const el = document.getElementById('dashboardToolSearch');
      return el ? el.value.trim().toLowerCase() : '';
    }

    function categoryAllowsCard(category, cardCategory, tool) {
      if (category === 'all') return true;
      if (category === 'favorites') return userFavorites.includes(tool);
      return cardCategory === category;
    }

    /** Applies current sidebar category + search box (title / description). */
    function applyDashboardFilters(options) {
      options = options || {};
      const animate = !!options.animate;
      const category = currentFilter;
      const term = getDashboardSearchTerm();
      const cards = document.querySelectorAll('.tool-card');

      function updateCards() {
        cards.forEach((card, index) => {
          const tool = card.getAttribute('data-tool');
          const cardCategory = card.getAttribute('data-category');
          const h3 = card.querySelector('h3');
          const p = card.querySelector('p');
          const title = h3 ? h3.textContent.toLowerCase() : '';
          const description = p ? p.textContent.toLowerCase() : '';

          const catOk = categoryAllowsCard(category, cardCategory, tool);
          const searchOk = !term || title.includes(term) || description.includes(term);
          const show = catOk && searchOk;

          if (show) {
            card.style.display = 'flex';
            card.style.order = (category === 'all' && userFavorites.includes(tool)) ? '0' : '1';
            if (animate) {
              card.style.opacity = '0';
              card.style.transform = 'translateY(20px)';
              setTimeout(() => {
                card.style.opacity = '1';
                card.style.transform = 'translateY(0)';
              }, index * 25);
            } else {
              card.style.opacity = '1';
              card.style.transform = 'translateY(0)';
            }
          } else {
            card.style.display = 'none';
          }
        });
        updateCategoryCounts();
        syncDashSections();
        fitCardTitles();
        var emptyEl = document.getElementById('dashboardEmptySearch');
        if (emptyEl) {
          var anyVisible = Array.prototype.some.call(cards, function(card) {
            return card.style.display !== 'none';
          });
          emptyEl.hidden = anyVisible || !term;
        }
      }

      if (animate) {
        cards.forEach(card => {
          card.style.opacity = '0';
          card.style.transform = 'translateY(20px)';
        });
        setTimeout(updateCards, 100);
      } else {
        updateCards();
      }
    }

    let dashboardSearchDebounce;

    function filterCategory(navEl, category) {
      currentFilter = category;

      document.querySelectorAll('.dashboard-wrapper .nav-item').forEach(item => {
        item.classList.remove('active');
      });
      if (navEl && navEl.classList) {
        navEl.classList.add('active');
      }

      applyDashboardFilters({ animate: true });
    }

    function syncDashSections() {
      document.querySelectorAll('.dash-section').forEach(function(section) {
        const cards = section.querySelectorAll('.tool-card');
        let visible = 0;
        cards.forEach(function(card) {
          if (card.style.display !== 'none') visible += 1;
        });
        section.classList.toggle('is-empty', visible === 0);
        const countEl = section.querySelector('.section-count');
        if (countEl) countEl.textContent = visible;
      });
    }
    
    // Initialize with smooth card entrance animation
    function updateCategoryCounts() {
      if (PORTAL_MINIMAL) return;
      const cards = document.querySelectorAll('.tool-card');
      const allCountEl = document.getElementById('allToolsCount');
      if (allCountEl) allCountEl.textContent = cards.length;
      const slugs = ['catalog', 'sales', 'inventory', 'warehouse', 'admin'];
      const counts = Object.fromEntries(slugs.map(s => [s, 0]));
      cards.forEach(card => {
        const cat = card.getAttribute('data-category');
        if (Object.prototype.hasOwnProperty.call(counts, cat)) counts[cat]++;
      });
      slugs.forEach(slug => {
        const el = document.getElementById(slug + 'Count');
        if (el) el.textContent = counts[slug];
      });
    }
    
    function fitCardTitles() {
      if (PORTAL_MINIMAL) return;
      document.querySelectorAll('.dashboard-wrapper .tool-card h3').forEach(function(el) {
        const card = el.closest('.tool-card');
        if (!card || card.style.display === 'none' || !el.clientWidth) return;

        card.style.removeProperty('--title-size');
        el.style.overflowWrap = 'normal';
        el.style.wordBreak = 'keep-all';
        el.style.hyphens = 'none';
        el.style.whiteSpace = 'normal';
        el.style.transition = 'none';

        // Shrink only when a single word is wider than the title box.
        // Short titles stay full size; two words can share one row.
        const min = 12;
        let size = parseFloat(getComputedStyle(el).fontSize);
        while (el.scrollWidth > el.clientWidth + 1 && size > min) {
          size -= 0.5;
          card.style.setProperty('--title-size', size + 'px');
        }
        el.style.transition = '';
      });
    }

    let fitTitlesDebounce;
    function scheduleFitCardTitles() {
      clearTimeout(fitTitlesDebounce);
      fitTitlesDebounce = setTimeout(fitCardTitles, 120);
    }

    const fdcBgCrop = { img: null, scale: 1, panX: 0, panY: 0, minScale: 0.1, maxScale: 8, fillScale: 1, fitScale: 1, dragging: false, lastX: 0, lastY: 0 };

    function dashboardBgUrl(file) {
      const base = window.DASHBOARD_STATIC_BG_BASE || '/static/__BG__';
      return String(base).replace('__BG__', file);
    }

    function dashboardBgCssUrl(bg) {
      if (!bg) return '';
      if (bg.url) return bg.url;
      if (bg.file) return dashboardBgUrl(bg.file);
      return '';
    }

    function applyDashboardBackground(bg) {
      if (!bg) return;
      currentDashboardBgId = bg.id;
      const cssUrl = dashboardBgCssUrl(bg);
      const wrapper = document.querySelector('.dashboard-wrapper');
      if (wrapper && cssUrl) {
        wrapper.style.setProperty('--fdc-dashboard-bg', 'url("' + cssUrl.replace(/"/g, '\\"') + '")');
      }
      const preload = document.getElementById('fdcBgPreload');
      if (preload && cssUrl) preload.href = cssUrl;
      const catalogCredit = document.getElementById('fdcPhotoCreditCatalog');
      const customCredit = document.getElementById('fdcPhotoCreditCustom');
      const isCustom = bg.id === 'custom' || !bg.photographer_url;
      if (catalogCredit) catalogCredit.hidden = !!isCustom;
      if (customCredit) {
        customCredit.hidden = !isCustom;
        const customDate = document.getElementById('fdcPhotoDateCustom');
        if (customDate) customDate.textContent = bg.date || '';
      }
      if (!isCustom) {
        const author = document.getElementById('fdcPhotoAuthor');
        const unsplash = document.getElementById('fdcPhotoUnsplash');
        const dateEl = document.getElementById('fdcPhotoDate');
        if (author) {
          author.href = bg.photographer_url;
          author.textContent = bg.photographer;
        }
        if (unsplash) unsplash.href = bg.photo_url;
        if (dateEl) dateEl.textContent = bg.date;
      }
      document.querySelectorAll('.fdc-bg-option').forEach(function(btn) {
        btn.classList.toggle('selected', btn.getAttribute('data-bg-id') === bg.id);
      });
    }

    function saveDashboardBackground(bgId) {
      try { localStorage.setItem('fdcDashboardBg', bgId); } catch (err) {}
      fetch('/save_dashboard_background', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: bgId})
      }).catch(function() {});
    }

    function setBgPickerMode(picker, mode) {
      picker.classList.remove('is-source', 'is-layout', 'is-cropping');
      if (mode) picker.classList.add(mode);
      const title = document.getElementById('fdcBgPickerTitle');
      if (!title) return;
      if (mode === 'is-source') title.textContent = title.dataset.source || 'Upload or paste a URL';
      else if (mode === 'is-layout') title.textContent = title.dataset.layout || 'Choose layout';
      else if (mode === 'is-cropping') title.textContent = title.dataset.crop || 'Zoom to fit the crop box';
      else title.textContent = title.dataset.choose || 'Choose background';
    }

    function setBgStatus(message) {
      const el = document.getElementById('fdcBgStatus');
      if (el) el.textContent = message || '';
    }

    function cropFrameSize() {
      const frame = document.getElementById('fdcBgCropFrame');
      if (!frame) return {w: 1, h: 1};
      const aspect = Math.max(0.5, Math.min(3.2, window.innerWidth / Math.max(window.innerHeight, 1)));
      const w = frame.clientWidth || 640;
      const h = Math.round(w / aspect);
      frame.style.height = h + 'px';
      return {w: frame.clientWidth || w, h: h};
    }

    function applyCropTransform() {
      const img = document.getElementById('fdcBgCropImg');
      if (!img) return;
      img.style.transform = 'translate(' + fdcBgCrop.panX + 'px, ' + fdcBgCrop.panY + 'px) scale(' + fdcBgCrop.scale + ')';
      const range = document.getElementById('fdcBgZoomRange');
      if (range) {
        const t = (fdcBgCrop.scale - fdcBgCrop.minScale) / Math.max(0.0001, fdcBgCrop.maxScale - fdcBgCrop.minScale);
        range.value = String(Math.round(10 + t * 390));
      }
    }

    function setCropScale(next, aroundCenter) {
      const frame = cropFrameSize();
      const nextScale = Math.max(fdcBgCrop.minScale, Math.min(fdcBgCrop.maxScale, next));
      if (aroundCenter !== false) {
        const cx = (frame.w / 2 - fdcBgCrop.panX) / fdcBgCrop.scale;
        const cy = (frame.h / 2 - fdcBgCrop.panY) / fdcBgCrop.scale;
        fdcBgCrop.scale = nextScale;
        fdcBgCrop.panX = frame.w / 2 - cx * fdcBgCrop.scale;
        fdcBgCrop.panY = frame.h / 2 - cy * fdcBgCrop.scale;
      } else {
        fdcBgCrop.scale = nextScale;
      }
      applyCropTransform();
    }

    function startCropEditor(dataUrl, layout) {
      const img = document.getElementById('fdcBgCropImg');
      const picker = document.getElementById('fdcBgPicker');
      if (!img || !picker) return;
      setBgPickerMode(picker, 'is-cropping');
      img.onload = function() {
        fdcBgCrop.img = img;
        const frame = cropFrameSize();
        const nw = img.naturalWidth || 1;
        const nh = img.naturalHeight || 1;
        fdcBgCrop.fillScale = Math.max(frame.w / nw, frame.h / nh);
        fdcBgCrop.fitScale = Math.min(frame.w / nw, frame.h / nh);
        fdcBgCrop.minScale = Math.max(0.05, fdcBgCrop.fitScale * 0.25);
        fdcBgCrop.maxScale = Math.max(fdcBgCrop.fillScale * 8, fdcBgCrop.fitScale * 8);
        const useFill = layout !== 'fit';
        fdcBgCrop.scale = useFill ? fdcBgCrop.fillScale : fdcBgCrop.fitScale;
        fdcBgCrop.panX = (frame.w - nw * fdcBgCrop.scale) / 2;
        fdcBgCrop.panY = (frame.h - nh * fdcBgCrop.scale) / 2;
        applyCropTransform();
      };
      img.src = dataUrl;
    }

    function exportCropBlob() {
      return new Promise(function(resolve, reject) {
        const img = document.getElementById('fdcBgCropImg');
        if (!img || !img.naturalWidth) {
          reject(new Error('No photo loaded'));
          return;
        }
        const frame = cropFrameSize();
        const outW = 1920;
        const outH = Math.max(1, Math.round(outW * (frame.h / Math.max(frame.w, 1))));
        const canvas = document.createElement('canvas');
        canvas.width = outW;
        canvas.height = outH;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#000';
        ctx.fillRect(0, 0, outW, outH);
        const ratio = outW / frame.w;
        ctx.drawImage(
          img,
          fdcBgCrop.panX * ratio,
          fdcBgCrop.panY * ratio,
          img.naturalWidth * fdcBgCrop.scale * ratio,
          img.naturalHeight * fdcBgCrop.scale * ratio
        );
        canvas.toBlob(function(blob) {
          if (!blob) reject(new Error('Could not crop photo'));
          else resolve(blob);
        }, 'image/jpeg', 0.86);
      });
    }

    function initDashboardBackgroundPicker() {
      const picker = document.getElementById('fdcBgPicker');
      const grid = document.getElementById('fdcBgGrid');
      const openBtn = document.getElementById('fdcBgCustomizeBtn');
      const uploadBtn = document.getElementById('fdcBgUploadBtn');
      const closeBtn = document.getElementById('fdcBgPickerClose');
      const fileBtn = document.getElementById('fdcBgFileBtn');
      const fileInput = document.getElementById('fdcBgFileInput');
      const urlInput = document.getElementById('fdcBgUrlInput');
      const urlBtn = document.getElementById('fdcBgUrlBtn');
      const frame = document.getElementById('fdcBgCropFrame');
      const zoomIn = document.getElementById('fdcBgZoomIn');
      const zoomOut = document.getElementById('fdcBgZoomOut');
      const zoomRange = document.getElementById('fdcBgZoomRange');
      if (!picker || !grid || !openBtn) return;

      const uploadTile = document.createElement('button');
      uploadTile.type = 'button';
      uploadTile.className = 'fdc-bg-option fdc-bg-upload-tile';
      uploadTile.innerHTML = '<i class="fas fa-upload"></i><span>Upload or URL</span>';
      uploadTile.addEventListener('click', function() {
        setBgStatus('');
        setBgPickerMode(picker, 'is-source');
      });
      grid.appendChild(uploadTile);

      DASHBOARD_BACKGROUNDS.forEach(function(bg) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'fdc-bg-option' + (bg.id === currentDashboardBgId ? ' selected' : '');
        btn.setAttribute('data-bg-id', bg.id);
        btn.innerHTML = '<img src="' + dashboardBgCssUrl(bg) + '" alt="">' +
          '<span>' + bg.label + '<br>' + bg.photographer + '</span>';
        btn.addEventListener('click', function() {
          applyDashboardBackground(bg);
          saveDashboardBackground(bg.id);
          closePicker();
        });
        grid.appendChild(btn);
      });

      function openPicker(mode) {
        picker.hidden = false;
        picker.classList.add('open');
        setBgPickerMode(picker, mode || '');
      }
      function closePicker() {
        picker.classList.remove('open');
        picker.hidden = true;
        setBgPickerMode(picker, '');
        setBgStatus('');
      }
      openBtn.addEventListener('click', function() { openPicker(''); });
      if (uploadBtn) uploadBtn.addEventListener('click', function() { openPicker('is-source'); });
      if (closeBtn) closeBtn.addEventListener('click', closePicker);
      picker.addEventListener('click', function(ev) {
        if (ev.target === picker) closePicker();
      });
      document.addEventListener('keydown', function(ev) {
        if (ev.key === 'Escape' && picker.classList.contains('open')) {
          if (picker.classList.contains('is-cropping')) setBgPickerMode(picker, 'is-layout');
          else if (picker.classList.contains('is-layout')) setBgPickerMode(picker, 'is-source');
          else if (picker.classList.contains('is-source')) setBgPickerMode(picker, '');
          else closePicker();
        }
      });

      function readFileAsDataUrl(file) {
        return new Promise(function(resolve, reject) {
          if (!file) {
            reject(new Error('Choose a photo first.'));
            return;
          }
          if (file.size > 12 * 1024 * 1024) {
            reject(new Error('Photo must be 12 MB or smaller.'));
            return;
          }
          const reader = new FileReader();
          reader.onload = function() { resolve(String(reader.result || '')); };
          reader.onerror = function() { reject(new Error('Could not read that file.')); };
          reader.readAsDataURL(file);
        });
      }

      if (fileBtn && fileInput) {
        fileBtn.addEventListener('click', function() { fileInput.click(); });
        fileInput.addEventListener('change', function() {
          const file = fileInput.files && fileInput.files[0];
          setBgStatus('Loading photo…');
          readFileAsDataUrl(file).then(function(dataUrl) {
            setBgStatus('');
            picker.dataset.cropSrc = dataUrl;
            setBgPickerMode(picker, 'is-layout');
          }).catch(function(err) {
            setBgStatus(err.message || 'Could not load that photo.');
          }).finally(function() {
            fileInput.value = '';
          });
        });
      }

      if (urlBtn && urlInput) {
        function loadUrl() {
          const url = (urlInput.value || '').trim();
          if (!url) {
            setBgStatus('Paste an image URL first.');
            return;
          }
          setBgStatus('Downloading photo…');
          fetch('/dashboard_background_from_url', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url: url})
          }).then(function(resp) { return resp.json().then(function(body) { return {ok: resp.ok, body: body}; }); })
            .then(function(result) {
              if (!result.ok || !result.body || !result.body.data_url) {
                throw new Error((result.body && result.body.error) || 'Could not load that URL.');
              }
              setBgStatus('');
              picker.dataset.cropSrc = result.body.data_url;
              setBgPickerMode(picker, 'is-layout');
            }).catch(function(err) {
              setBgStatus(err.message || 'Could not load that URL.');
            });
        }
        urlBtn.addEventListener('click', loadUrl);
        urlInput.addEventListener('keydown', function(ev) {
          if (ev.key === 'Enter') {
            ev.preventDefault();
            loadUrl();
          }
        });
      }

      document.querySelectorAll('.fdc-bg-layout-opt').forEach(function(btn) {
        btn.addEventListener('click', function() {
          const src = picker.dataset.cropSrc;
          if (!src) {
            setBgPickerMode(picker, 'is-source');
            setBgStatus('Upload a photo or paste a URL first.');
            return;
          }
          startCropEditor(src, btn.getAttribute('data-layout') || 'fill');
        });
      });

      const backBtn = document.getElementById('fdcBgCropBack');
      if (backBtn) backBtn.addEventListener('click', function() {
        setBgPickerMode(picker, 'is-layout');
      });
      const applyBtn = document.getElementById('fdcBgCropApply');
      if (applyBtn) applyBtn.addEventListener('click', function() {
        applyBtn.disabled = true;
        exportCropBlob().then(function(blob) {
          const body = new FormData();
          body.append('image', blob, 'background.jpg');
          return fetch('/upload_dashboard_background', {method: 'POST', body: body});
        }).then(function(resp) { return resp.json().then(function(json) { return {ok: resp.ok, json: json}; }); })
          .then(function(result) {
            if (!result.ok || !result.json || !result.json.success) {
              throw new Error((result.json && result.json.error) || 'Could not save background.');
            }
            applyDashboardBackground(result.json.background);
            closePicker();
          }).catch(function(err) {
            setBgStatus(err.message || 'Could not save background.');
            setBgPickerMode(picker, 'is-source');
          }).finally(function() {
            applyBtn.disabled = false;
          });
      });

      if (zoomIn) zoomIn.addEventListener('click', function() { setCropScale(fdcBgCrop.scale * 1.12); });
      if (zoomOut) zoomOut.addEventListener('click', function() { setCropScale(fdcBgCrop.scale / 1.12); });
      if (zoomRange) zoomRange.addEventListener('input', function() {
        const t = (Number(zoomRange.value) - 10) / 390;
        setCropScale(fdcBgCrop.minScale + t * (fdcBgCrop.maxScale - fdcBgCrop.minScale));
      });
      if (frame) {
        frame.addEventListener('wheel', function(ev) {
          ev.preventDefault();
          setCropScale(fdcBgCrop.scale * (ev.deltaY < 0 ? 1.08 : 1 / 1.08));
        }, {passive: false});
        frame.addEventListener('pointerdown', function(ev) {
          fdcBgCrop.dragging = true;
          fdcBgCrop.lastX = ev.clientX;
          fdcBgCrop.lastY = ev.clientY;
          frame.classList.add('is-dragging');
          frame.setPointerCapture(ev.pointerId);
        });
        frame.addEventListener('pointermove', function(ev) {
          if (!fdcBgCrop.dragging) return;
          fdcBgCrop.panX += ev.clientX - fdcBgCrop.lastX;
          fdcBgCrop.panY += ev.clientY - fdcBgCrop.lastY;
          fdcBgCrop.lastX = ev.clientX;
          fdcBgCrop.lastY = ev.clientY;
          applyCropTransform();
        });
        function stopDrag() {
          fdcBgCrop.dragging = false;
          frame.classList.remove('is-dragging');
        }
        frame.addEventListener('pointerup', stopDrag);
        frame.addEventListener('pointercancel', stopDrag);
      }

      if (!window.DASHBOARD_BG_FROM_SERVER) {
        try {
          const savedId = localStorage.getItem('fdcDashboardBg');
          const savedBg = (typeof DASHBOARD_BACKGROUNDS !== 'undefined' ? DASHBOARD_BACKGROUNDS : []).find(function(item) { return item.id === savedId; });
          if (savedBg) applyDashboardBackground(savedBg);
        } catch (err) {}
      }
    }

    window.toggleFavorite = toggleFavorite;
    window.filterCategory = filterCategory;
    
    document.addEventListener('DOMContentLoaded', function() {
      initDashboardBackgroundPicker();
      if (PORTAL_MINIMAL) {
        document.querySelectorAll('.tool-card').forEach(function(card) {
          card.style.display = 'flex';
          card.style.opacity = '1';
        });
        syncDashSections();
        return;
      }
      loadFavorites().then(() => {
        applyDashboardFilters({ animate: false });
        fitCardTitles();
      });

      const searchEl = document.getElementById('dashboardToolSearch');
      if (searchEl) {
        searchEl.addEventListener('input', function() {
          clearTimeout(dashboardSearchDebounce);
          dashboardSearchDebounce = setTimeout(function() {
            applyDashboardFilters({ animate: false });
          }, 140);
        });
      }

      window.addEventListener('resize', scheduleFitCardTitles);
      if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(fitCardTitles);
      }
    });
