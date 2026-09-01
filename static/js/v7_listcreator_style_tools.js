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

    function bootStyleTools() {
        window.syncRemoveStylesQtyControls();
        window.updateCurrentStyleList();
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootStyleTools);
    } else {
        bootStyleTools();
    }
})();
