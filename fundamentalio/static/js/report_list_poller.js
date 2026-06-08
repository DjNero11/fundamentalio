(function() {
    function registerReportListPoller() {
        if (window.__reportListPollerRegistered) {
            return;
        }
        window.__reportListPollerRegistered = true;

        Alpine.data('reportListPoller', function() {
            return {
                pollIntervalId: null,
                statusUrl: '',

                init: function() {
                    var root = this.$el;
                    this.statusUrl = root.getAttribute('data-status-url') || '';
                    if (this._inProcessIds().length > 0) {
                        this.poll();
                        this.pollIntervalId = setInterval(this.poll.bind(this), 5000);
                    }
                    window.addEventListener('beforeunload', this._clearInterval.bind(this));
                },

                _clearInterval: function() {
                    if (this.pollIntervalId !== null) {
                        clearInterval(this.pollIntervalId);
                        this.pollIntervalId = null;
                    }
                },

                _inProcessIds: function() {
                    var root = this.$el;
                    var nodes = root.querySelectorAll('[data-report-status="in_process"]');
                    var ids = [];
                    nodes.forEach(function(node) {
                        var id = node.getAttribute('data-report-id');
                        if (id) {
                            ids.push(id);
                        }
                    });
                    return ids;
                },

                poll: async function() {
                    var ids = this._inProcessIds();
                    if (!ids.length || !this.statusUrl) {
                        this._clearInterval();
                        return;
                    }

                    try {
                        var resp = await fetch(
                            this.statusUrl + '?ids=' + encodeURIComponent(ids.join(',')),
                            {
                                method: 'GET',
                                credentials: 'same-origin',
                                headers: { 'Accept': 'application/json' },
                            }
                        );
                        if (!resp.ok) {
                            return;
                        }
                        var contentType = resp.headers.get('content-type') || '';
                        if (contentType.indexOf('application/json') === -1) {
                            return;
                        }
                        var data = await resp.json();
                        var reports = (data && data.reports) ? data.reports : [];
                        var self = this;
                        reports.forEach(function(report) {
                            if (report.status !== 'in_process') {
                                self._updateRow(report);
                            }
                        });
                    } catch (e) {
                        return;
                    }

                    if (this._inProcessIds().length === 0) {
                        this._clearInterval();
                    }
                },

                _getRowMeta: function(rowEl) {
                    return {
                        id: rowEl.getAttribute('data-report-id') || '',
                        companyName: rowEl.getAttribute('data-company-name') || '',
                        companySymbol: rowEl.getAttribute('data-company-symbol') || '',
                        typeDisplay: rowEl.getAttribute('data-type-display') || '',
                        createdDate: rowEl.getAttribute('data-created-date') || '',
                        detailUrl: rowEl.getAttribute('data-detail-url') || '',
                    };
                },

                _updateRow: function(report) {
                    var root = this.$el;
                    var rowEl = root.querySelector('[data-report-id="' + report.id + '"]');
                    if (!rowEl) {
                        return;
                    }
                    var meta = this._getRowMeta(rowEl);
                    var html = this._renderRowHtml(meta, report.status, report.read);
                    var wrapper = document.createElement('div');
                    wrapper.innerHTML = html.trim();
                    var newEl = wrapper.firstElementChild;
                    if (newEl) {
                        rowEl.replaceWith(newEl);
                    }
                },

                _renderRowHtml: function(meta, status, read) {
                    var createdBlock =
                        '<div class="text-right">' +
                        '<p class="text-xs text-gray-400 uppercase tracking-wide">Created</p>' +
                        '<p class="text-sm font-medium text-gray-700">' + this._escapeHtml(meta.createdDate) + '</p>' +
                        '</div>';

                    var subtitle =
                        '<p class="mt-0.5 text-sm text-gray-400">' +
                        this._escapeHtml(meta.companySymbol) + ' · ' + this._escapeHtml(meta.typeDisplay) +
                        '</p>';

                    var dataAttrs =
                        ' data-report-id="' + this._escapeHtml(meta.id) + '"' +
                        ' data-report-status="' + this._escapeHtml(status) + '"' +
                        ' data-company-name="' + this._escapeHtml(meta.companyName) + '"' +
                        ' data-company-symbol="' + this._escapeHtml(meta.companySymbol) + '"' +
                        ' data-type-display="' + this._escapeHtml(meta.typeDisplay) + '"' +
                        ' data-created-date="' + this._escapeHtml(meta.createdDate) + '"' +
                        ' data-detail-url="' + this._escapeHtml(meta.detailUrl) + '"';

                    if (status === 'in_process') {
                        return (
                            '<div' + dataAttrs +
                            ' data-testid="report-row report-row-in-process"' +
                            ' class="block rounded-xl border border-gray-200 bg-white px-4 py-4 sm:px-5 sm:py-4 shadow-sm cursor-default">' +
                            '<div class="flex items-start justify-between gap-3">' +
                            '<div>' +
                            '<h2 class="text-base sm:text-lg font-semibold text-gray-900">' + this._escapeHtml(meta.companyName) + '</h2>' +
                            '<div class="mt-1 flex items-center gap-2 text-sm text-gray-500">' +
                            '<span class="inline-block h-4 w-4 rounded-full border-2 border-gray-300 border-t-gray-600 animate-spin" aria-hidden="true"></span>' +
                            '<span data-testid="generating-label">Generating report</span>' +
                            '</div>' + subtitle +
                            '</div>' + createdBlock +
                            '</div></div>'
                        );
                    }

                    if (status === 'error') {
                        return (
                            '<div' + dataAttrs +
                            ' data-testid="report-row report-row-error"' +
                            ' class="block rounded-xl border border-gray-200 bg-white px-4 py-4 sm:px-5 sm:py-4 shadow-sm cursor-default">' +
                            '<div class="flex items-start justify-between gap-3">' +
                            '<div>' +
                            '<h2 class="text-base sm:text-lg font-semibold text-gray-900">' + this._escapeHtml(meta.companyName) + '</h2>' +
                            '<p class="mt-1 text-sm text-red-400" data-testid="error-label">error occurred during generation</p>' +
                            subtitle +
                            '</div>' + createdBlock +
                            '</div></div>'
                        );
                    }

                    var unreadTestId = 'unread' + '-dot';
                    var dot = (!read)
                        ? '<span class="inline-flex h-2 w-2 rounded-full bg-blue-500" aria-hidden="true" data-testid="' + unreadTestId + '"></span>'
                        : '';

                    return (
                        '<a href="' + this._escapeHtml(meta.detailUrl) + '"' + dataAttrs +
                        ' data-testid="report-row report-row-done"' +
                        ' class="block rounded-xl border border-gray-200 bg-white px-4 py-4 sm:px-5 sm:py-4 shadow-sm hover:shadow-md transition-shadow">' +
                        '<div class="flex items-start justify-between gap-3">' +
                        '<div>' +
                        '<div class="flex items-center gap-2">' +
                        '<h2 class="text-base sm:text-lg font-semibold text-gray-900">' + this._escapeHtml(meta.companyName) + '</h2>' +
                        dot +
                        '</div>' +
                        '<p class="mt-0.5 text-sm text-gray-500">' +
                        this._escapeHtml(meta.companySymbol) + ' · ' + this._escapeHtml(meta.typeDisplay) +
                        '</p>' +
                        '</div>' + createdBlock +
                        '</div></a>'
                    );
                },

                _escapeHtml: function(value) {
                    return String(value)
                        .replace(/&/g, '&amp;')
                        .replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;')
                        .replace(/"/g, '&quot;');
                },
            };
        });
    }

    document.addEventListener('alpine:init', registerReportListPoller);
})();
