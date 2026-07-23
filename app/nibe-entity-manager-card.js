/**
 * nibe-entity-manager-card.js
 *
 * Home Assistant Lovelace custom card for managing Nibe heat-pump entities
 * published by the generate_nibe_mqtt.py bridge.
 *
 * Architecture
 * ------------
 * The card is a Web Component (HTMLElement with Shadow DOM) registered as
 * 'nibe-entity-manager-card'.  It communicates exclusively through MQTT via
 * HA's WebSocket connection — no direct API calls.
 *
 * MQTT topic contract (all topics are under the nibe/ namespace):
 *
 *   Subscribed (read):
 *     nibe/browser/all_metadata      Retained batch metadata for ALL points in one
 *                                    message (keyed by string point ID). Subscribed
 *                                    at startup; replaces the previous per-point
 *                                    meta/# fan-out for bulk loading.
 *     nibe/browser/meta/#            Retained per-point metadata; used for individual
 *                                    dynamic appear/disappear updates after startup.
 *                                    An empty payload means the point was removed.
 *     nibe/browser/enabled_state     Retained list of currently enabled point IDs.
 *     nibe/browser/dynamic           Non-retained real-time change event (toast).
 *     nibe/browser/changelog/history Retained full changelog (gzip-compressed).
 *                                    Payload prefixed with "gzip1:" + gzip bytes.
 *     nibe/browser/changelog/unread  Retained unread count for the badge.
 *
 *   Published (write, via HA mqtt.publish service):
 *     homeassistant/text/nibe_enable_entity/set   Point ID to enable.
 *     homeassistant/text/nibe_disable_entity/set  Point ID to disable.
 *     homeassistant/button/nibe_mark_changes_read/press  Mark changelog read.
 *
 * Responsive layout
 * -----------------
 * ≥ 601 px: desktop table view with sortable columns and inline filter row.
 * ≤ 600 px: mobile card view with a collapsible filter/sort panel.
 * Both views share the same underlying filteredEntities array and selection state.
 */

// Shared constant — entity type → human-readable label.
// Defined at module scope so renderTable() and renderMobileCards() stay in sync
// without duplicating the mapping.
// Cache busting is handled automatically by the bridge: it computes a SHA-256
// hash of this file and appends it as a query parameter when registering the
// Lovelace resource (e.g. ?v=c4faeaad815f). The browser loads a fresh copy
// whenever the file changes, without any manual version number maintenance.

const TYPE_DISPLAY_NAMES = {
  binary_sensor: 'Binary Sensor',
  text:          'Text',
  text_sensor:   'Text Sensor',
  sensor:        'Sensor',
  number:        'Number',
  select:        'Select',
  switch:        'Switch',
  button:        'Button',
  time:          'Time',
  date:          'Date',
};

class NibeEntityManager extends HTMLElement {
  /**
   * Initialise all instance state.  Shadow DOM is attached here so the card
   * renders even before HA calls setConfig() or sets the hass property.
   * MQTT subscriptions are deferred until hass is available (set hass setter).
   */
  constructor() {
    super();
    
    this.entities = new Map();
    this.filteredEntities = [];
    this.selectedIds = new Set();
    this.dynamicEntityIds = new Set();
    
    this.config = {
      title:                 '',
      mqttTopicPrefix:       'nibe/browser/meta/',
      mqttEnabledStateTopic: 'nibe/browser/enabled_state',
      mqttDynamicTopic:      'nibe/browser/dynamic',
      pageSize:              50,
      suppressInitialToasts: true,
    };
    
    this.currentPage = 0;
    this.searchTerm = '';
    this.sortField = 'id';
    this.sortAscending = true;
    this.isLoading = true;
    
    this.typeFilter    = '';
    this.statusFilter  = '';
    this.writableFilter = '';
    this.dynamicFilter = '';  // '' | 'dynamic' | 'static'

    // Device model from bridge — used in UI text instead of hardcoded 'SMO S40'
    this.deviceModel = 'Nibe controller';
    
    this.updateTimeout = null;
    this.debounceTime = 200;

    // Fuse.js fuzzy search — loaded lazily when hass first becomes available.
    // _fuseIndex is rebuilt whenever the full entity set changes (all_metadata).
    // Falls back to substring matching until Fuse is loaded or if load fails.
    this._fuse = null;
    this._fuseLoaded = false;
    this._fuseResultIds   = null;  // Set<id> for O(1) membership during filter
    this._fuseResultOrder = null;  // Map<id, rank> for score-ordered sort

    this.mqttSubscriptions = [];
    this.changelog = [];
    this.changelogCap = null;  // set by bridge via total_entries in changelog payload
    this.snapshots = [];       // list of snapshot objects from nibe/browser/snapshots
    this.appliedMode = '';     // current applied mode from nibe/browser/applied_mode
    this.unreadChanges = 0;

    // Stores the last received enabled_state set so handleAllMetadataMessage
    // can apply correct enabled flags even when all_metadata arrives after
    // enabled_state (message delivery order is not guaranteed).
    this._lastKnownEnabledPoints = null;

    this._hass = null;
    this.eventListenersSet = false;
    this._openModalId = null;  // tracks which modal is currently visible

    this.attachShadow({ mode: 'open' });

    this.mqttSetupDone = false;
    this.showMobileFilters = false;
  }

  /**
   * Called by HA when the card configuration changes.
   * Merges user-supplied config over the defaults and rebuilds the DOM.
   * Event listeners are re-attached every time because render() replaces
   * the entire innerHTML, destroying all previously registered handlers.
   *
   * @param {Object} config - Lovelace card config from ui-lovelace.yaml.
   */
  setConfig(config) {
    if (config) {
      Object.assign(this.config, config);
    }
    this.render();
    // Re-attach DOM event listeners every time the skeleton is rebuilt.
    this.setupEventListeners();
    this.setupMobileEventListeners();
    this.eventListenersSet = true;
  }

  render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          font-family: var(--ha-font-family, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif);
        }
        
        .container {
          padding: 16px;
          background: var(--card-background-color, white);
          border-radius: 12px;
          box-shadow: 0px 2px 4px rgba(0, 0, 0, 0.1);
          color: var(--primary-text-color, #212121);
        }
        
        .header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 20px;
          flex-wrap: wrap;
          gap: 12px;
        }
        
        .title {
          font-size: 20px;
          font-weight: 500;
          margin: 0;
        }
        
        .stats {
          display: flex;
          gap: 12px;
          font-size: 12px;
          flex-wrap: wrap;
        }
        
        .stat {
          padding: 6px 12px;
          background: var(--secondary-background-color, #f5f5f5);
          border-radius: 16px;
          border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
        }
        
        .stat-value {
          font-weight: 600;
          color: var(--primary-color, #03a9f4);
        }
        
        .controls {
          display: flex;
          gap: 12px;
          margin-bottom: 16px;
          flex-wrap: wrap;
        }
        
        .search-container {
          flex: 1;
          min-width: 200px;
          position: relative;
        }
        
        .search-box {
          width: 100%;
          padding: 8px 36px 8px 12px;
          border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
          border-radius: 8px;
          font-size: 14px;
          background: var(--card-background-color, white);
          color: var(--primary-text-color, #212121);
          box-sizing: border-box;
        }
        
        .search-clear {
          position: absolute;
          right: 8px;
          top: 50%;
          transform: translateY(-50%);
          background: none;
          border: none;
          cursor: pointer;
          padding: 4px;
          font-size: 16px;
          width: 24px;
          height: 24px;
          opacity: 0.6;
        }
        
        .search-clear:hover {
          opacity: 1;
        }
        
        .search-clear:disabled {
          opacity: 0.3;
          cursor: not-allowed;
        }
        
        .button {
          padding: 8px 16px;
          border: none;
          border-radius: 12px; 
          font-size: 14px;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.2s;
          white-space: nowrap;
        }
        
        .button-primary {
          background: var(--primary-color, #03a9f4);
          color: white;
        }
        
        .button-secondary {
          background: var(--secondary-background-color, #e5e5e5);
          color: var(--primary-text-color, #212121);
          border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
        }
        
        .button-success {
          background: #43a047;
          color: white;
        }
        
        .button-danger {
          background: #db4437;
          color: white;
        }
        
        .button-warning {
          background: #f4b400;
          color: white;
        }
        
        .button-small {
          padding: 6px 10px;
          font-size: 12px;
          border-radius: 8px;
        }
        
        .button:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }
        
        .button:hover:not(:disabled) {
          box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
          transform: translateY(-1px);
        }
        
        /* Mobile Filter Bar */
        .mobile-filter-bar {
          display: none;
          margin-bottom: 16px;
        }
        
        .mobile-filter-toggle {
          width: 100%;
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 10px 14px;  /* Slightly reduced padding */
          background: var(--secondary-background-color, #f5f5f5);
          border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
          border-radius: 10px;  /* Slightly smaller radius */
          font-size: 14px;
          font-weight: 500;
          cursor: pointer;
          box-sizing: border-box; /* Ensures padding doesn't add to width */
        }
        
        .mobile-filter-panel {
          display: none; /* Always start hidden, JS controls visibility */
          margin-top: 12px;
          padding: 16px;
          background: var(--secondary-background-color, #f5f5f5);
          border-radius: 12px;
        }
        
        .mobile-filter-group {
          margin-bottom: 16px;
        }
        
        .mobile-filter-label {
          display: block;
          margin-bottom: 6px;
          font-size: 12px;
          font-weight: 600;
          color: var(--secondary-text-color, #727272);
          text-transform: uppercase;
        }
        
        /* Mobile filter selects */
        .mobile-filter-select {
          width: 100%;
          padding: 12px 14px; /* Slightly larger for touch */
          border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
          border-radius: 10px;
          background: var(--card-background-color, white);
          font-size: 16px; /* Better for mobile readability */
        }
        
        .mobile-filter-actions {
          display: flex;
          gap: 12px;
          margin-top: 8px;
        }
        
        .mobile-filter-actions .button {
          flex: 1;
        }
        
        /* Desktop Table */
        .table-container {
          border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
          border-radius: 12px;
          overflow: hidden;
          max-height: 70vh;
          overflow-y: auto;
        }
        
        .entity-table {
          width: 100%;
          border-collapse: collapse;
          font-size: 14px;
        }
        
        .entity-table th {
          background: var(--primary-color, #03a9f4);
          padding: 12px 8px;
          text-align: left;
          font-weight: 500;
          color: white;
          position: sticky;
          top: 0;
          cursor: pointer;
          user-select: none;
          font-size: 12px;
          text-transform: uppercase;
        }
        
        .entity-table td {
          padding: 12px 8px;
          border-bottom: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
        }
        
        .entity-table tbody tr:hover {
          background: var(--secondary-background-color, #f5f5f5);
        }

        .entity-table td:nth-child(6) {
            width: 150px;
            min-width: 150px;
            max-width: 150px;
        }
        
        /* Mobile Cards */
        .mobile-cards {
          display: none;
        }
        
        .entity-card {
          background: var(--card-background-color, white);
          border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
          border-radius: 12px;
          padding: 12px;
          margin-bottom: 8px;
        }
        
        .card-row {
          display: flex;
          align-items: center;
          margin-bottom: 8px;
        }
        
        .card-row:last-child {
          margin-bottom: 0;
        }
        
        .card-checkbox {
          margin-right: 12px;
        }
        
        .checkbox-large {
          width: 22px;
          height: 22px;
          accent-color: var(--primary-color, #03a9f4);
          cursor: pointer;
        }
        
        .card-id {
          font-family: monospace;
          font-weight: 700;
          font-size: 16px;
          color: var(--primary-color, #03a9f4);
          margin-right: 8px;
        }
        
        .card-title {
          font-size: 15px;
          font-weight: 500;
          flex: 1;
          min-width: 0;
          overflow-wrap: break-word;
          word-break: break-word;
          color: var(--primary-text-color, #212121);
        }
        
        .card-badges {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          align-items: center;
        }
        
        .card-actions {
          display: flex;
          gap: 8px;
          margin-top: 4px;
        }
        
        .card-button {
          flex: 1;
          padding: 10px 8px;
          border: none;
          border-radius: 10px;
          font-size: 14px;
          font-weight: 500;
          cursor: pointer;
          text-align: center;
        }
        
        .card-button-success {
          background: #43a047;
          color: white;
        }
        
        .card-button-danger {
          background: #db4437;
          color: white;
        }
        
        .card-button-secondary {
          background: var(--secondary-background-color, #e5e5e5);
          color: var(--primary-text-color, #212121);
          border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
        }
        
        .detail-badge {
          display: inline-block;
          padding: 3px 8px;
          border-radius: 10px;
          font-size: 11px;
          font-weight: 600;
          color: white;
          text-transform: uppercase;
          white-space: nowrap;
          vertical-align: middle;
          margin: 2px 2px 2px 0;
        }
        .badge {
          display: inline-block;
          padding: 3px 8px;
          border-radius: 10px;
          font-size: 11px;
          font-weight: 600;
          color: white;
          text-transform: uppercase;
          white-space: nowrap;
          vertical-align: middle;
        }

        /* Wrapper so type/writable/dynamic badges sit side-by-side in the
           table type cell, wrapping only when the column is genuinely narrow. */
        .badge-group {
          display: flex;
          flex-wrap: wrap;
          gap: 4px;
          align-items: center;
        }

        .badge-sensor { background: #0DA035; }
        .badge-binary_sensor { background: #039be5; }
        .badge-number { background: #ff9800; }
        .badge-select { background: #f4b400; }
        .badge-switch { background: #DF4C1E; }
        .badge-text { background: #9e9e9e; }
        .badge-button { background: #44739e; }
        .badge-text_sensor { background: #7e57c2; }
        .badge-time { background: #26a69a; }
        .badge-date { background: #26a69a; }
        .badge-datetime { background: #26a69a; }
        .badge-enabled { background: #43a047; }
        .badge-disabled { background: #9e9e9e; }
        .badge-writable {
          background: var(--primary-color, #03a9f4);
        }
        
        .page-info {
          font-size: 12px;
          color: var(--secondary-text-color, #727272);
          flex: 1;
          min-width: 0;
          overflow-wrap: break-word;
        }

        .page-buttons {
          display: flex;
          gap: 8px;
          margin-left: auto;
        }

        /* Consistent 14px breathing room — matches the space between the
           filter row and the table, and between the table and prev/next. */
        .filter-row {
          margin-bottom: 14px;
        }

        .pagination {
          margin-top: 14px;
          margin-bottom: 6px;
        }
        
        .loading, .empty {
          text-align: center;
          padding: 40px;
          color: var(--secondary-text-color, #727272);
        }
        
        .toast-container {
          position: fixed;
          top: 80px;
          right: 20px;
          z-index: 1000;
          max-width: 300px;
        }
        
        .toast {
          padding: 12px 16px;
          margin-bottom: 8px;
          border-radius: 12px;
          font-size: 14px;
          font-weight: 500;
          box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
          opacity: 0;
          transform: translateX(100%);
          transition: all 0.3s ease;
          overflow-wrap: break-word;
          word-break: break-word;
        }
        
        .toast.show {
          opacity: 1;
          transform: translateX(0);
        }
        
        .toast-success { background: #43a047; color: white; }
        .toast-error { background: #db4437; color: white; }
        .toast-info { background: #03a9f4; color: white; }
        
        .modal {
          position: fixed;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: rgba(0, 0, 0, 0.5);
          z-index: 1001;
          display: none;
          align-items: center;
          justify-content: center;
          padding: 20px;
        }
        
        .modal.show {
          display: flex;
        }
        
        .modal-content {
          background: var(--card-background-color, white);
          border-radius: 12px;
          max-width: 500px;
          width: 100%;
          max-height: 80vh;
          overflow-y: auto;
          box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
        }
        
        .modal-header {
          padding: 16px 20px;
          border-bottom: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
          display: flex;
          justify-content: space-between;
          align-items: center;
        }
        
        .modal-title {
          margin: 0;
          font-size: 20px;
          font-weight: 500;
          min-width: 0;
          overflow-wrap: break-word;
          word-break: break-word;
        }
        
        .modal-close {
          background: none;
          border: none;
          font-size: 24px;
          cursor: pointer;
          padding: 0;
          width: 30px;
          height: 30px;
        }
        
        .modal-body {
          padding: 20px;
        }
        
        .entity-details {
          display: grid;
          gap: 12px;
        }
        
        .detail-row {
          display: flex;
          gap: 12px;
        }
        
        .detail-label {
          font-weight: 600;
          min-width: 120px;
          flex-shrink: 0;
          color: var(--secondary-text-color, #727272);
        }
        
        .detail-value {
          flex: 1;
          min-width: 0;
          overflow-wrap: break-word;
          word-break: break-word;
        }
        
        .actions-container {
            display: flex;
            gap: 6px;
            min-width: 130px;
            max-width: 130px;
        }
        
        .button-fixed {
            flex: 1;
            min-width: 60px;
            max-width: 60px;
            text-align: center;
            padding: 6px 4px !important;
            font-size: 12px;
            border-radius: 8px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .button-fixed-success {
            background: #43a047;
            color: white;
            border: none;
        }
        
        .button-fixed-danger {
            background: #db4437;
            color: white;
            border: none;
        }
        
        .button-fixed-secondary {
            background: var(--secondary-background-color, #e5e5e5);
            color: var(--primary-text-color, #212121);
            border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
        }

        .button-fixed:hover:not(:disabled) {
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
            transform: translateY(-2px);
        }
        
        .button-fixed:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none !important;
            box-shadow: none !important;
        }
        
        /* Responsive Breakpoint */
        @media screen and (max-width: 600px) {
          .filter-row {
            display: none;
          }
          
          .mobile-filter-bar {
            display: block;
          }
          
          .table-container {
            display: none;
          }
          
          .mobile-cards {
            display: block;
          }
          
          .controls .button {
            padding: 8px 12px;
            font-size: 13px;
          }
          
          .stats {
            width: 100%;
            justify-content: space-between;
          }

          .mobile-filter-toggle {
            padding: 8px 12px;  /* Even more compact on small screens */
            font-size: 13px;
            margin: 0;  /* Remove any default margins */
          }
          
          .mobile-filter-toggle span:first-child {
            white-space: nowrap;  /* Prevents "Filter & Sort" from wrapping */
          }
          
          .mobile-filter-toggle span:last-child {
            padding-left: 8px;  /* Space for the arrow */
          }

          .search-box {
            font-size: 16px;  /* Prevents iOS zoom */
          }
        }
        
        @media screen and (min-width: 601px) {
          .mobile-filter-bar {
            display: none;
          }
          
          .mobile-cards {
            display: none;
          }
          
          .table-container {
            display: block;
          }
        }

        /* Extra small screens - stack everything */
        @media screen and (max-width: 480px) {
          .filter-row {
            flex-direction: column;
            gap: 8px;
          }
          
          .filter-select {
            width: 100%;
            flex: 1 1 100%;
            max-width: 100%;
          }
          
          #clear-filters {
            width: 100%;
          }
        }
      </style>
      
      <div class="container">
        <div class="toast-container"></div>
        
        <div class="header">
          ${this.config.title ? `<h1 class="title">${this._esc(this.config.title)}</h1>` : ''}
          <div class="stats">
            <div class="stat">
              <span title="Total data points known to the bridge (all discovered registers)">Total</span>:
              <span class="stat-value" id="total-count">0</span>
            </div>
            <div class="stat">
              <span title="Data points currently enabled in the bridge and visible as HA entities">Enabled</span>:
              <span class="stat-value" id="enabled-count">0</span>
            </div>
            <div class="stat" id="selection-stat" style="display: none;">
              <span title="Data points currently selected for bulk enable/disable operations">Selected</span>:
              <span class="stat-value" id="selected-count">0</span>
            </div>
          </div>
        </div>
        
        <div class="controls">
          <div class="search-container">
            <input type="text" class="search-box" placeholder="Search..." id="search-input">
            <button class="search-clear" id="search-clear" disabled>&times;</button>
          </div>
          
          <button class="button button-secondary" id="select-all" title="Select all data points on the current filtered page for bulk enable/disable operations">Select All</button>
          <button class="button button-warning" id="clear-selection" title="Deselect all selected data points" disabled>Clear</button>
          <button class="button button-success" id="enable-selected" title="Enable all selected data points — they will appear as Home Assistant entities" disabled>Enable</button>
          <button class="button button-danger" id="disable-selected" title="Disable all selected static data points — dynamic points are skipped (they are controlled by the firmware)" disabled>Disable</button>
          <button class="button button-secondary" id="show-changelog" title="View the history of dynamic data point appearances and disappearances, and HA entity disable events">Changelog</button>
          <button class="button button-secondary" id="show-snapshots" title="Save and restore named snapshots of your enabled entity selection">Snapshots</button>
        </div>
        
        <!-- Desktop Filter Row -->
        <div class="filter-row">
          <select class="filter-select" id="type-filter">
            <option value="">All Types</option>
            <option value="sensor">Sensor</option>
            <option value="binary_sensor">Binary Sensor</option>
            <option value="number">Number</option>
            <option value="select">Select</option>
            <option value="switch">Switch</option>
            <option value="text">Text</option>
            <option value="text_sensor">Text Sensor</option>
            <option value="button">Button</option>
            <option value="date">Date</option>
            <option value="time">Time</option>
          </select>
          
          <select class="filter-select" id="status-filter">
            <option value="">All Status</option>
            <option value="enabled">Enabled</option>
            <option value="disabled">Disabled</option>
          </select>
          
          <select class="filter-select" id="writable-filter"
                  title="Filter by access type: writable entities send commands to the controller">
            <option value="">All Access</option>
            <option value="true">Writable</option>
            <option value="false">Read-only</option>
          </select>

          <select class="filter-select" id="dynamic-filter"
                  title="Dynamic entities appear only when a related operating mode is active — they cannot be disabled manually">
            <option value="">All Points</option>
            <option value="dynamic">Dynamic only</option>
            <option value="static">Static only</option>
          </select>

          <button class="button button-secondary" id="clear-filters">Clear Filters</button>
        </div>
        
        <!-- Mobile Filter Bar -->
        <div class="mobile-filter-bar">
          <div class="mobile-filter-toggle" id="mobile-filter-toggle">
            <span>🔍 Filter & Sort</span>
            <span id="mobile-filter-indicator">▼</span>
          </div>
          <div class="mobile-filter-panel" id="mobile-filter-panel">
            <div class="mobile-filter-group">
              <div class="mobile-filter-label">Entity Type</div>
              <select class="mobile-filter-select" id="mobile-type-filter">
                <option value="">All Types</option>
                <option value="sensor">Sensor</option>
                <option value="binary_sensor">Binary Sensor</option>
                <option value="number">Number</option>
                <option value="select">Select</option>
                <option value="switch">Switch</option>
                <option value="text">Text</option>
                <option value="text_sensor">Text Sensor</option>
                <option value="button">Button</option>
                <option value="date">Date</option>
                <option value="time">Time</option>
              </select>
            </div>
            
            <div class="mobile-filter-group">
              <div class="mobile-filter-label">Status</div>
              <select class="mobile-filter-select" id="mobile-status-filter">
                <option value="">All Status</option>
                <option value="enabled">Enabled</option>
                <option value="disabled">Disabled</option>
              </select>
            </div>
            
            <div class="mobile-filter-group">
              <div class="mobile-filter-label">Access</div>
              <select class="mobile-filter-select" id="mobile-writable-filter">
                <option value="">All Access</option>
                <option value="true">Writable</option>
                <option value="false">Read-only</option>
              </select>
            </div>

            <div class="mobile-filter-group">
              <div class="mobile-filter-label">Point Type</div>
              <select class="mobile-filter-select" id="mobile-dynamic-filter">
                <option value="">All Points</option>
                <option value="dynamic">Dynamic only</option>
                <option value="static">Static only</option>
              </select>
            </div>
            
            <div class="mobile-filter-group">
              <div class="mobile-filter-label">Sort By</div>
              <select class="mobile-filter-select" id="mobile-sort-filter">
                <option value="id-asc">ID (Low to High)</option>
                <option value="id-desc">ID (High to Low)</option>
                <option value="title-asc">Title (A-Z)</option>
                <option value="title-desc">Title (Z-A)</option>
                <option value="type-asc">Type (A-Z)</option>
                <option value="type-desc">Type (Z-A)</option>
                <option value="enabled-desc">Enabled First</option>
                <option value="enabled-asc">Disabled First</option>
              </select>
            </div>
            
            <div class="mobile-filter-actions">
              <button class="button button-secondary" id="mobile-apply-filters">Apply</button>
              <button class="button button-secondary" id="mobile-clear-filters">Clear</button>
            </div>
          </div>
        </div>
        
        <!-- Desktop Table View -->
        <div class="table-container">
          <table class="entity-table">
            <thead>
              <tr>
                <th style="width: 30px;">
                  <input type="checkbox" id="select-all-checkbox" class="checkbox">
                </th>
                <th data-sort="id" style="width: 80px;">ID</th>
                <th data-sort="type" style="width: 100px;">Type</th>
                <th data-sort="title">Title</th>
                <th data-sort="enabled" style="width: 100px;">Status</th>
                <th style="width: 120px;">Actions</th>
              </tr>
            </thead>
            <tbody id="entity-table-body">
              <tr>
                <td colspan="6" class="loading">Loading entities...</td>
              </tr>
            </tbody>
          </table>
        </div>
        
        <!-- Mobile Card View -->
        <div class="mobile-cards" id="mobile-cards-container">
          <!-- Cards will be rendered here -->
        </div>
        
        <div class="pagination" style="display:flex;align-items:center;width:100%;">
          <div class="page-info">
            Showing <span id="page-start">0</span>–<span id="page-end">0</span> of <span id="total-filtered">0</span>
          </div>
          <div class="page-buttons">
            <button class="button button-secondary" id="prev-page"
                    title="Previous page" disabled>&#8592; Previous</button>
            <button class="button button-secondary" id="next-page"
                    title="Next page" disabled>Next &#8594;</button>
          </div>
        </div>
      </div>
      
      <div class="modal" id="changelog-modal">
        <div class="modal-content">
          <div class="modal-header">
            <h2 class="modal-title">Dynamic Changes</h2>
            <button class="modal-close" id="close-changelog">&times;</button>
          </div>
          <div class="modal-body" id="changelog-content"></div>
        </div>
      </div>

      <div class="modal" id="snapshots-modal">
        <div class="modal-content">
          <div class="modal-header">
            <h2 class="modal-title">Snapshots</h2>
            <button class="modal-close" id="close-snapshots">&times;</button>
          </div>
          <div class="modal-body" id="snapshots-content">
            <div id="snapshots-save-section" style="margin-bottom:20px;padding:16px;
                 background:var(--ha-color-secondary,var(--secondary-background-color));
                 border-radius:6px;">
              <div style="font-weight:600;margin-bottom:10px;color:var(--primary-text-color);">
                Save current selection
              </div>
              <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                <input type="text" id="snapshot-name-input"
                  placeholder="Snapshot name (e.g. Summer, Commissioning…)"
                  style="flex:1;min-width:180px;padding:8px 10px;border-radius:4px;
                         border:1px solid var(--divider-color,#e0e0e0);
                         background:var(--card-background-color,#fff);
                         color:var(--primary-text-color);font-size:14px;"
                  maxlength="40">
                <button class="button button-success" id="snapshot-save-btn"
                  title="Save the current enabled entity selection as a named snapshot">
                  Save
                </button>
              </div>
              <div id="snapshot-save-msg" style="margin-top:8px;font-size:13px;
                   color:var(--ha-color-secondary-text,#888);min-height:18px;"></div>
            </div>
            <div id="snapshots-list"></div>
          </div>
        </div>
      </div>
      
      <div class="modal" id="details-modal">
        <div class="modal-content">
          <div class="modal-header">
            <h2 class="modal-title">Entity Details</h2>
            <button class="modal-close" id="close-details">&times;</button>
          </div>
          <div class="modal-body" id="details-content"></div>
        </div>
      </div>
    `;
  }

  /**
   * Called by the browser when the element is inserted into the DOM.
   * Guards against duplicate listener registration across hot-reloads.
   * MQTT setup runs here only if hass was already set; otherwise the
   * hass setter handles it on first assignment.
   */
  connectedCallback() {
    if (!this.eventListenersSet) {
      this.setupEventListeners();
      this.setupMobileEventListeners();
      this.eventListenersSet = true;
    }
    
    if (this._hass && !this.mqttSetupDone) {
      this.setupMqttSubscriptions();
      this.mqttSetupDone = true;
    }
  }

  /**
   * HA sets this property after connectedCallback on initial load and again
   * whenever the HA connection is re-established.  MQTT subscriptions are
   * established here on first call because hass.connection is not available
   * until this point.
   *
   * @param {Object} hass - The HA hass object with connection and callService.
   */
  set hass(hass) {
    this._hass = hass;
    if (!this.mqttSetupDone) {
      this.setupMqttSubscriptions();
      this.mqttSetupDone = true;
      this._loadFuse();
    }
  }

  /**
   * Unsubscribe from all active MQTT subscriptions.
   * Called from disconnectedCallback() to avoid memory leaks and stale
   * callbacks when the card is removed from the dashboard.
   *
   * hass.connection.subscribeMessage() returns a Promise that resolves to
   * the actual unsubscribe function — not the function itself. Each entry
   * in mqttSubscriptions is therefore awaited before being invoked.
   */
  cleanupSubscriptions() {
    this.mqttSubscriptions.forEach(subPromise => {
      Promise.resolve(subPromise)
        .then(unsubscribe => {
          if (typeof unsubscribe === 'function') unsubscribe();
        })
        .catch(() => {});
    });
    this.mqttSubscriptions = [];
  }

  /**
   * Attach event listeners to the desktop UI controls.
   * Must be called after every render() because innerHTML replacement
   * destroys all previously registered DOM listeners.
   */
  setupEventListeners() {
    this.shadowRoot.getElementById('search-input')?.addEventListener('input', (e) => {
      this.searchTerm = e.target.value;
      this.currentPage = 0;
      this.updateSearchClearButton();
      this.debouncedUpdate();
    });

    this.shadowRoot.getElementById('search-clear')?.addEventListener('click', () => {
      this.searchTerm = '';
      this.shadowRoot.getElementById('search-input').value = '';
      this.updateSearchClearButton();
      this.debouncedUpdate();
    });

    ['type-filter', 'status-filter', 'writable-filter', 'dynamic-filter'].forEach(id => {
      this.shadowRoot.getElementById(id)?.addEventListener('change', (e) => {
        this[id.replace('-filter', 'Filter')] = e.target.value;
        this.currentPage = 0;
        this.updateTable();
      });
    });

    const buttons = {
      'select-all':       () => this.selectAll(),
      'clear-selection':  () => this.clearSelection(),
      'enable-selected':  () => this.enableSelected(),
      'disable-selected': () => this.disableSelected(),
      'show-changelog':   () => this.showChangelog(),
      'show-snapshots':   () => this.showSnapshots(),
      'close-snapshots':  () => this.hideModal('snapshots-modal'),
      'snapshot-save-btn': () => this._handleSnapshotSave(),
      'clear-filters':    () => this.clearFilters(),
      'prev-page':        () => this.previousPage(),
      'next-page':        () => this.nextPage(),
      'close-changelog':  () => this.hideModal('changelog-modal'),
      'close-details':    () => this.hideModal('details-modal')
    };

    Object.entries(buttons).forEach(([id, handler]) => {
      this.shadowRoot.getElementById(id)?.addEventListener('click', handler);
    });

    // The select-all checkbox toggles every entity on the current filtered page.
    this.shadowRoot.getElementById('select-all-checkbox')?.addEventListener('change', (e) => {
      this.filteredEntities.forEach(entity => {
        e.target.checked ? this.selectedIds.add(entity.id) : this.selectedIds.delete(entity.id);
      });
      this.updateTable();
    });

    // Clicking a column header toggles sort direction; clicking a new header
    // switches the sort field and resets to ascending.
    this.shadowRoot.querySelectorAll('th[data-sort]').forEach(header => {
      header.addEventListener('click', () => {
        const field = header.dataset.sort;
        if (this.sortField === field) {
          this.sortAscending = !this.sortAscending;
        } else {
          this.sortField = field;
          this.sortAscending = true;
        }
        this.updateTable();
      });
    });
  }

  /**
   * Attach event listeners to the mobile filter panel controls.
   * The panel applies all filter/sort selections together on "Apply" rather
   * than triggering a re-render on every individual change, reducing layout
   * thrash on slower mobile devices.
   */
  setupMobileEventListeners() {
    const toggle = this.shadowRoot.getElementById('mobile-filter-toggle');
    if (toggle) {
      toggle.addEventListener('click', () => {
        this.showMobileFilters = !this.showMobileFilters;
        const panel = this.shadowRoot.getElementById('mobile-filter-panel');
        const indicator = this.shadowRoot.getElementById('mobile-filter-indicator');
        if (panel) panel.style.display = this.showMobileFilters ? 'block' : 'none';
        if (indicator) indicator.textContent = this.showMobileFilters ? '▲' : '▼';
      });
    }

    this.shadowRoot.getElementById('mobile-apply-filters')?.addEventListener('click', () => {
      this.typeFilter     = this.shadowRoot.getElementById('mobile-type-filter')?.value || '';
      this.statusFilter   = this.shadowRoot.getElementById('mobile-status-filter')?.value || '';
      this.writableFilter = this.shadowRoot.getElementById('mobile-writable-filter')?.value || '';
      this.dynamicFilter  = this.shadowRoot.getElementById('mobile-dynamic-filter')?.value  || '';

      // Mirror the applied values onto the desktop dropdowns so switching
      // from mobile to desktop view shows the correct active filter state.
      this.setElementValue('type-filter',     this.typeFilter);
      this.setElementValue('status-filter',   this.statusFilter);
      this.setElementValue('writable-filter', this.writableFilter);
      this.setElementValue('dynamic-filter',  this.dynamicFilter);

      const sortValue = this.shadowRoot.getElementById('mobile-sort-filter')?.value || 'id-asc';
      const [field, order] = sortValue.split('-');

      if (field === 'enabled') {
        this.sortField = 'enabled';
        this.sortAscending = order === 'asc';
      } else if (field === 'id' || field === 'title' || field === 'type') {
        this.sortField = field;
        this.sortAscending = order === 'asc';
      }

      this.currentPage = 0;
      this.showMobileFilters = false;
      const panel = this.shadowRoot.getElementById('mobile-filter-panel');
      const indicator = this.shadowRoot.getElementById('mobile-filter-indicator');
      if (panel) panel.style.display = 'none';
      if (indicator) indicator.textContent = '▼';

      this.updateTable();
    });

    this.shadowRoot.getElementById('mobile-clear-filters')?.addEventListener('click', () => {
      // Reset mobile select elements to their default options
      this.shadowRoot.getElementById('mobile-type-filter').value     = '';
      this.shadowRoot.getElementById('mobile-status-filter').value   = '';
      this.shadowRoot.getElementById('mobile-writable-filter').value = '';
      this.shadowRoot.getElementById('mobile-dynamic-filter').value  = '';
      this.shadowRoot.getElementById('mobile-sort-filter').value     = 'id-asc';

      this.typeFilter     = '';
      this.statusFilter   = '';
      this.writableFilter = '';
      this.dynamicFilter  = '';
      this.sortField      = 'id';
      this.sortAscending  = true;

      this.currentPage = 0;
      this.showMobileFilters = false;
      const panel = this.shadowRoot.getElementById('mobile-filter-panel');
      const indicator = this.shadowRoot.getElementById('mobile-filter-indicator');
      if (panel) panel.style.display = 'none';
      if (indicator) indicator.textContent = '▼';

      this.updateTable();
    });
  }

  /**
   * Decompress a changelog payload produced by the Python bridge.
   *
   * Payloads are prefixed with the ASCII sentinel "gzip1:" followed by
   * base64-encoded gzip bytes.  Decodes and decompresses using the
   * DecompressionStream API (available in all modern browsers and HA's
   * frontend environment).
   *
   * @param {string} payload - Raw MQTT payload string from HA WebSocket.
   * @returns {Promise<string>} Resolved JSON string, ready for JSON.parse().
   */
  async _decompressPayload(payload) {
    // Payload format: "gzip1:<base64-encoded gzip bytes>"
    const SENTINEL = 'gzip1:';

    // Decode the base64 portion to get the raw gzip bytes.
    const b64     = payload.slice(SENTINEL.length);
    const binary  = atob(b64);
    const bytes   = Uint8Array.from(binary, c => c.charCodeAt(0));

    const ds     = new DecompressionStream('gzip');
    const writer = ds.writable.getWriter();
    const reader = ds.readable.getReader();

    writer.write(bytes);
    writer.close();

    const chunks = [];
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      chunks.push(value);
    }

    const total  = chunks.reduce((n, c) => n + c.length, 0);
    const result = new Uint8Array(total);
    let   offset = 0;
    for (const chunk of chunks) {
      result.set(chunk, offset);
      offset += chunk.length;
    }
    return new TextDecoder().decode(result);
  }

  /**
   * Lazy-load Fuse.js from cdnjs and build the initial search index.
   * Called once from the hass setter when HA first becomes available.
   * No-ops silently if Fuse is already loaded or if the script fails —
   * getFilteredEntities() falls back to substring matching in that case.
   */
  async _loadFuse() {
    if (this._fuseLoaded) return;
    try {
      await new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = 'https://cdnjs.cloudflare.com/ajax/libs/fuse.js/7.0.0/fuse.min.js';
        script.onload  = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
      });
      this._fuseLoaded = true;
      this._rebuildFuseIndex();
    } catch (e) {
      console.warn('Nibe card: Fuse.js failed to load, using substring search.', e);
    }
  }

  /**
   * Rebuild the Fuse.js search index from the current entity Map.
   * Called after all_metadata is processed and whenever the entity set changes.
   * Indexes on title only — unit and ID use exact substring matching separately.
   */
  _rebuildFuseIndex() {
    if (!this._fuseLoaded || typeof Fuse === 'undefined') return;
    const items = Array.from(this.entities.values());
    this._fuse = new Fuse(items, {
      keys:             ['title'],
      threshold:        0.35,   // 0=exact match, 1=match anything; 0.35 is firm but typo-tolerant
      distance:         100,    // how far into the string the match can be
      minMatchCharLength: 3,
      includeScore:     false,
      ignoreLocation:   true,   // match anywhere in the title, not just the start
    });
  }

  /**
   * Subscribe to all required MQTT topics via HA's WebSocket connection.
   * All subscriptions are stored in this.mqttSubscriptions so they can be
   * cleanly unsubscribed when the card is removed (disconnectedCallback).
   *
   * Called once, guarded by mqttSetupDone, so reconnects don't create
   * duplicate subscriptions.
   */
  setupMqttSubscriptions() {
    if (!this._hass) return;
  
    try {
      this.mqttSubscriptions.push(
        // Batch metadata: one retained message with all point metadata keyed
        // by string point ID.  Replaces the previous nibe/browser/meta/# fan-out
        // for the initial bulk load (performance Finding 8).
        this._hass.connection.subscribeMessage(
          (msg) => this.handleAllMetadataMessage(msg),
          { type: 'mqtt/subscribe', topic: 'nibe/browser/all_metadata' }
        ),
        // Per-point metadata: still subscribed for individual dynamic
        // appear/disappear updates that happen after the initial load.
        this._hass.connection.subscribeMessage(
          (msg) => this.handleMetadataMessage(msg),
          { type: 'mqtt/subscribe', topic: `${this.config.mqttTopicPrefix}#` }
        ),
        this._hass.connection.subscribeMessage(
          (msg) => this.handleEnabledStateMessage(msg),
          { type: 'mqtt/subscribe', topic: this.config.mqttEnabledStateTopic }
        ),
        this._hass.connection.subscribeMessage(
          (msg) => this.handleDynamicChangeMessage(msg),
          { type: 'mqtt/subscribe', topic: this.config.mqttDynamicTopic }
        ),
        this._hass.connection.subscribeMessage(
          (msg) => this.handleChangelogHistoryMessage(msg),
          { type: 'mqtt/subscribe', topic: 'nibe/browser/changelog/history' }
        ),
        this._hass.connection.subscribeMessage(
          (msg) => this.handleChangelogUnreadMessage(msg),
          { type: 'mqtt/subscribe', topic: 'nibe/browser/changelog/unread' }
        ),
        this._hass.connection.subscribeMessage(
          (msg) => this.handleSnapshotsMessage(msg),
          { type: 'mqtt/subscribe', topic: 'nibe/browser/snapshots' }
        ),
        this._hass.connection.subscribeMessage(
          (msg) => this.handleAppliedModeMessage(msg),
          { type: 'mqtt/subscribe', topic: 'nibe/browser/applied_mode' }
        ),
        this._hass.connection.subscribeMessage(
          (msg) => this.handleDeviceInfoMessage(msg),
          { type: 'mqtt/subscribe', topic: 'nibe/browser/device_info' }
        ),
        this._hass.connection.subscribeMessage(
          (msg) => this.handlePointListMessage(msg),
          { type: 'mqtt/subscribe', topic: 'nibe/browser/point_list' }
        )
      );
  
    } catch (error) {
      console.error('MQTT setup failed:', error);
    }
  }

  /**
   * Handle the retained nibe/browser/all_metadata batch message.
   *
   * The bridge publishes all ~1063 point metadata records as a single JSON
   * object keyed by string point ID.  Parsing one message here replaces the
   * previous approach of handling ~1063 individual nibe/browser/meta/{id}
   * messages at startup, reducing broker and WebSocket round-trips
   * significantly (performance Finding 8).
   *
   * Each record is normalised through the same field mapping used by
   * handleMetadataMessage() so the entity shape in this.entities is
   * identical regardless of which path populated it.
   *
   * @param {Object} msg - MQTT message with topic and payload fields.
   */
  handleAllMetadataMessage(msg) {
    try {
      if (!msg.payload) return;

      const data = JSON.parse(msg.payload);
      if (!data.metadata || typeof data.metadata !== 'object') return;

      // Clear dynamic set before repopulating — a full all_metadata message
      // is authoritative.  Without this, dynamic points removed from firmware
      // would persist in dynamicEntityIds across broker reconnects (Finding 3).
      // Only clear when we have a full batch (count > 0) to avoid wiping on
      // an empty or malformed message.
      const incomingCount = Object.keys(data.metadata).length;
      if (incomingCount > 0) {
        this.dynamicEntityIds.clear();
      }

      let updated = 0;
      for (const [idStr, metadata] of Object.entries(data.metadata)) {
        const pointId = parseInt(idStr, 10);
        if (isNaN(pointId)) continue;

        const existingEntity = this.entities.get(pointId);

        // Determine enabled state with correct priority (Finding 1):
        //   1. _lastKnownEnabledPoints — set by handleEnabledStateMessage
        //      regardless of which message arrived first.
        //   2. Existing entity's enabled flag — preserves optimistic updates.
        //   3. False — safe default for genuinely new points.
        let enabled = false;
        if (this._lastKnownEnabledPoints) {
          enabled = this._lastKnownEnabledPoints.has(pointId);
        } else if (existingEntity) {
          enabled = existingEntity.enabled;
        }

        const entity = {
          id:                 pointId,
          title:              metadata.title || `Point ${pointId}`,
          type:               metadata.type  || 'sensor',
          writable:           Boolean(metadata.writable),
          enabled,
          isDynamic:          Boolean(metadata.is_dynamic),
          unit:               metadata.unit        || '',
          unitOverridden:     Boolean(metadata.unit_overridden),
          unitRaw:            metadata.unit_raw    || '',
          shortUnit:          metadata.shortUnit   || '',
          category:           metadata.category    || '',
          description:        metadata.description || '',
          modbusRegisterID:   metadata.modbusRegisterID   || null,
          modbusRegisterType: metadata.modbusRegisterType || '',
          variableType:       metadata.variableType || '',
          variableSize:       metadata.variableSize || '',
          minValue:           metadata.min_value !== undefined ? metadata.min_value : null,
          maxValue:           metadata.max_value !== undefined ? metadata.max_value : null,
          divisor:            metadata.divisor  !== undefined ? metadata.divisor  : 1,
          decimal:            metadata.decimal  !== undefined ? metadata.decimal  : 0,
          change:             metadata.change   !== undefined ? metadata.change   : 0,
          intDefaultValue:    metadata.intDefaultValue     !== undefined ? metadata.intDefaultValue : null,
          stringDefaultValue: metadata.stringDefaultValue  || '',
          lastUpdated:        Date.now(),
        };

        this.entities.set(pointId, entity);
        if (entity.isDynamic) {
          this.dynamicEntityIds.add(pointId);
        }
        updated++;
      }

      if (updated > 0) {
        this.isLoading = false;
        this._rebuildFuseIndex();
        this.debouncedUpdate();
      }
    } catch (e) {
      console.error('Error processing all_metadata:', e);
    }
  }

  /**
   * Handle the retained nibe/browser/device_info message.
   * Updates this.deviceModel so UI text reflects the actual connected device
   * (e.g. 'SMO S40', 'VVM S320', 'S1155') instead of a hardcoded string.
   */
  handleDeviceInfoMessage(msg) {
    try {
      if (!msg.payload) return;
      const info = JSON.parse(msg.payload);
      if (info.model) {
        this.deviceModel = info.model;
      }
    } catch (e) {
      console.warn('Failed to parse device_info:', e);
    }
  }

  /**
   * Handle the retained nibe/browser/point_list message.
   *
   * This topic carries the authoritative set of point IDs the bridge knows
   * about. The card uses it to reconcile its local entities Map:
   *   - Point IDs in the list but absent from the Map are added as stubs
   *     (the per-point meta subscription will fill in the details).
   *   - Point IDs in the Map but absent from the list are removed — they
   *     have disappeared from the firmware and their meta tombstone may
   *     have been sent before this card subscribed.
   *
   * This makes the total count display accurate immediately on load and
   * ensures deletions are never silently missed.
   *
   * @param {Object} msg - MQTT message with topic and payload fields.
   */
  handlePointListMessage(msg) {
    try {
      if (!msg.payload) return;
      const data = JSON.parse(msg.payload);
      if (!Array.isArray(data.points)) return;

      const authoritative = new Set(data.points);

      // Remove any entities the bridge no longer knows about.
      let removed = 0;
      for (const [pointId] of this.entities) {
        if (!authoritative.has(pointId)) {
          this.entities.delete(pointId);
          this.dynamicEntityIds.delete(pointId);
          removed++;
        }
      }

      // Add stubs for point IDs we haven't received metadata for yet.
      // Stubs use the same field names as full entities so filters/renderers
      // work consistently — `isDynamic` not `is_dynamic`, `writable` not
      // `is_writable`.  The enabled state is seeded from _lastKnownEnabledPoints
      // so stubs are correct even before all_metadata arrives (Finding 1).
      // The per-point meta subscription will overwrite stubs with full data.
      let added = 0;
      for (const pointId of authoritative) {
        if (!this.entities.has(pointId)) {
          this.entities.set(pointId, {
            id:          pointId,
            title:       `Point ${pointId}`,
            type:        'sensor',
            writable:    false,
            enabled:     this._lastKnownEnabledPoints
                           ? this._lastKnownEnabledPoints.has(pointId)
                           : false,
            isDynamic:   false,
            unit:        '',
            unitOverridden: false,
            unitRaw:     '',
            shortUnit:   '',
            category:    '',
            description: '',
            lastUpdated: Date.now(),
          });
          added++;
        }
      }

      if (removed > 0 || added > 0) {
        this.debouncedUpdate();
      }
    } catch (e) {
      console.warn('Failed to parse point_list:', e);
    }
  }

  /**
   * Handle a retained message from nibe/browser/changelog/history.
   * Replaces the full local changelog with the validated server copy and
   * updates the unread badge.  Entries missing required fields are silently
   * dropped to guard against corrupt data from a previous broker session.
   *
   * The payload is gzip-compressed, prefixed with "gzip1:", and decoded via
   * _decompressPayload.
   *
   * @param {Object} msg - MQTT message with topic and payload fields.
   */
  async handleChangelogHistoryMessage(msg) {
    try {
      if (!msg.payload) {
        this.changelog = [];
        return;
      }

      const jsonStr = await this._decompressPayload(msg.payload);
      const data    = JSON.parse(jsonStr);
      
      if (data.history && Array.isArray(data.history)) {
        const cleanHistory = [];
        
        for (const entry of data.history) {
          if (entry && typeof entry === 'object') {
            const cleanEntry = {
              timestamp:     entry.timestamp || Date.now() / 1000,
              iso_timestamp: entry.iso_timestamp || this.formatDateTimeHA(new Date((entry.timestamp || Date.now() / 1000) * 1000)),
              added:         Array.isArray(entry.added)
                               ? entry.added.filter(e => e && typeof e === 'object' && typeof e.id === 'number')
                               : [],
              removed:       Array.isArray(entry.removed)
                               ? entry.removed.filter(e => e && typeof e === 'object' && typeof e.id === 'number')
                               : [],
              id:            entry.id || `change_${Date.now()}`,
              unread:        Boolean(entry.unread),
              // source: 'firmware' = dynamic appear/disappear
              //         'ha_disabled' = user disabled via HA cog
              source:        entry.source || 'firmware',
              // triggered_by: {id, title} of the controlling point whose write
              // caused the scan, or null for startup / periodic poll changes
              triggered_by:  entry.triggered_by || null,
            };

            if (cleanEntry.added.length > 0 || cleanEntry.removed.length > 0) {
              cleanHistory.push(cleanEntry);
            }
          }
        }

        this.changelog = cleanHistory;

        if (typeof data.unread_count === 'number') {
          this.unreadChanges = data.unread_count;
          this.updateChangelogBadge();
        }
        // Reflect the server-configured cap in the footer
        if (typeof data.total_entries === 'number') {
          this.changelogCap = data.total_entries;
        }

        // If the changelog modal is open when fresh data arrives, refresh
        // its content in place so the user sees the latest entries without
        // having to close and reopen it.
        if (this._openModalId === 'changelog-modal') {
          this._renderChangelogContent();
        }
      }
    } catch (error) {
      console.error('Error loading changelog history:', error);
      this.changelog = [];
    }
  }
  
  /**
   * Handle a retained message from nibe/browser/changelog/unread.
   * Updates the badge count on the Changelog button without needing to
   * parse the full history payload.
   *
   * @param {Object} msg - MQTT message with topic and payload fields.
   */
  handleChangelogUnreadMessage(msg) {
    try {
      if (!msg.payload) return;
      
      const data = JSON.parse(msg.payload);
      
      if (typeof data.unread_count === 'number') {
        this.unreadChanges = data.unread_count;
        this.updateChangelogBadge();
      }
    } catch (error) {
      console.error('Error loading unread count:', error);
    }
  }
  
  /**
   * Handle a non-retained message from nibe/browser/dynamic.
   * Shows a toast for immediate user feedback only.  The local changelog
   * array is NOT updated here — the retained changelog/history topic is the
   * single source of truth and arrives shortly after via
   * handleChangelogHistoryMessage(), replacing the full list correctly.
   *
   * @param {Object} msg - MQTT message with topic and payload fields.
   */
  handleDynamicChangeMessage(msg) {
    try {
      if (!msg.payload) return;

      const changeEvent = JSON.parse(msg.payload);
      const added     = changeEvent.added?.length   || 0;
      const removed   = changeEvent.removed?.length || 0;
      const source    = changeEvent.source || 'firmware';
      const trig      = changeEvent.triggered_by || null;  // {id, title} or null

      let message = '';
      let toastType = 'info';

      if (source === 'ha_disabled') {
        // HA entity registry disable — single item always in removed[]
        const item = changeEvent.removed?.[0];
        message = item
          ? `HA disabled: ${item.title || `Point ${item.id}`}`
          : 'Entity disabled via HA entity registry';
        toastType = 'warning';
      } else {
        // Firmware-driven dynamic change
        if (added > 0 && removed > 0) {
          message = `${added} data point(s) appeared, ${removed} disappeared`;
        } else if (added > 0) {
          message = `${added} dynamic data point(s) appeared`;
        } else if (removed > 0) {
          message = `${removed} dynamic data point(s) disappeared`;
        }
        // Append the controlling point name when known
        if (trig && message) {
          message += ` (triggered by: ${trig.title || `Point ${trig.id}`})`;
        }
      }

      if (message) this.showToast(message, toastType);

    } catch (error) {
      console.error('Error processing dynamic change:', error);
    }
  }

  /**
   * Handle a retained message from nibe/browser/meta/{pointId}.
   * Creates or updates the entity in the local Map, or deletes it when
   * the bridge publishes an empty payload (point disappeared from firmware).
   * Preserves the entity's enabled state across metadata refreshes by
   * copying it from the existing entry if one exists.
   *
   * @param {Object} msg - MQTT message with topic and payload fields.
   */
  handleMetadataMessage(msg) {
    try {
      const pointId = parseInt(msg.topic.split('/').pop());
      if (isNaN(pointId)) return;
      
      if (!msg.payload || msg.payload.trim() === '') {
        if (this.entities.has(pointId)) {
          this.entities.delete(pointId);
          this.dynamicEntityIds.delete(pointId);
          this.debouncedUpdate();
        }
        return;
      }
      
      const metadata = JSON.parse(msg.payload);
      const existingEntity = this.entities.get(pointId);
      
      const entity = {
        id: pointId,
        title: metadata.title || `Point ${pointId}`,
        type: metadata.type || 'sensor',
        writable: Boolean(metadata.writable || false),
        enabled: existingEntity ? existingEntity.enabled : false,
        isDynamic: Boolean(metadata.is_dynamic || false),
        unit: metadata.unit || '',
        unitOverridden: Boolean(metadata.unit_overridden),
        unitRaw: metadata.unit_raw || '',
        shortUnit: metadata.shortUnit || '',
        category: metadata.category || '',
        description: metadata.description || '',
        modbusRegisterID: metadata.modbusRegisterID || null,
        modbusRegisterType: metadata.modbusRegisterType || '',
        variableType: metadata.variableType || '',
        variableSize: metadata.variableSize || '',
        minValue: metadata.min_value !== undefined ? metadata.min_value : null,
        maxValue: metadata.max_value !== undefined ? metadata.max_value : null,
        divisor: metadata.divisor !== undefined ? metadata.divisor : 1,
        decimal: metadata.decimal !== undefined ? metadata.decimal : 0,
        change: metadata.change !== undefined ? metadata.change : 0,
        intDefaultValue: metadata.intDefaultValue !== undefined ? metadata.intDefaultValue : null,
        stringDefaultValue: metadata.stringDefaultValue || '',
        lastUpdated: metadata.last_updated ? metadata.last_updated * 1000 : Date.now()
      };
      
      this.entities.set(pointId, entity);

      if (entity.isDynamic) {
        this.dynamicEntityIds.add(pointId);
      }

      // Re-render when a new entity arrives OR when is_dynamic changes on an
      // existing entity (dynamic points appear after the initial load and the
      // disable button must update from active to greyed-out).
      const dynamicChanged = existingEntity &&
        existingEntity.isDynamic !== entity.isDynamic;
      if (!existingEntity || dynamicChanged) {
        this.debouncedUpdate();
      }
      
    } catch (error) {
      console.error('Error processing metadata:', error);
    }
  }

  /**
   * Handle a retained message from nibe/browser/enabled_state.
   * Updates the enabled flag on every entity in the local Map by comparing
   * against the authoritative list published by the bridge.  Triggers a full
   * table re-render so enabled/disabled badges reflect the new state.
   *
   * The enabled_state message may arrive before all_metadata has populated
   * the entities Map (retained messages are delivered in subscription order,
   * and enabled_state is subscribed after all_metadata but brokers deliver
   * retained messages on subscribe — order is not guaranteed across sessions).
   * The enabled set is always stored in _lastKnownEnabledPoints so that
   * handleAllMetadataMessage can apply it when it populates entities later.
   *
   * @param {Object} msg - MQTT message with topic and payload fields.
   */
  handleEnabledStateMessage(msg) {
    try {
      if (!msg.payload) return;

      const data = JSON.parse(msg.payload);
      const enabledPoints = new Set(data.enabled_points || []);

      // Always persist — handleAllMetadataMessage reads this when creating
      // entities so enabled state is correct regardless of message order.
      this._lastKnownEnabledPoints = enabledPoints;

      this.entities.forEach((entity, id) => {
        entity.enabled = enabledPoints.has(id);
      });

      this.updateTable();
    } catch (error) {
      console.error('Error processing enabled state:', error);
    }
  }

  /**
   * Format a Date object using the HA locale when available, falling back
   * to the browser's default locale.
   *
   * HA exposes its locale through several different APIs depending on the
   * frontend version, so three fallback paths are tried in order:
   *   1. window.hassUtil.formatDateTime  (older HA versions)
   *   2. this._hass.formatDateTime       (mid-era HA versions)
   *   3. Intl / toLocaleString with hass.locale settings
   *
   * @param {Date|null} date - The date to format, or null for 'N/A'.
   * @returns {string} Localised date-time string.
   */
  formatDateTimeHA(date) {
    if (!date) return 'N/A';
    
    const dateObj = date instanceof Date ? date : new Date(date);
    
    if (window.hassUtil?.formatDateTime && this._hass?.locale) {
      return window.hassUtil.formatDateTime(dateObj, this._hass.locale);
    }
    
    if (this._hass?.formatDateTime) {
      return this._hass.formatDateTime(dateObj, this._hass.locale);
    }
    
    if (this._hass?.locale) {
      const locale = this._hass.locale.language || 'en';
      const options = {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: this._hass.locale.time_format !== '24'
      };
      return dateObj.toLocaleString(locale, options);
    }
    
    return dateObj.toLocaleString();
  }

  /**
   * Send enable commands to the bridge for a list of point IDs.
   * Commands are sent sequentially with a 50 ms gap to avoid flooding the
   * MQTT broker when a large selection is enabled at once.
   *
   * Optimistic update: entity.enabled is set to true immediately in the
   * local Map so the UI reflects the change without waiting for the
   * bridge's enabled_state response (which can take up to one poll cycle).
   * The bridge is authoritative — handleEnabledStateMessage will reconcile
   * if the bridge rejects any enable.
   *
   * @param {number[]} pointIds - Array of Nibe variableId values to enable.
   */
  async enableEntities(pointIds) {
    if (!pointIds.length || !this._hass) return;

    // Optimistic update — reflect in UI immediately.
    for (const pointId of pointIds) {
      const entity = this.entities.get(pointId);
      if (entity) entity.enabled = true;
    }
    this.updateTable();

    let succeeded = 0;
    let anyFailed = false;
    for (const pointId of pointIds) {
      try {
        await this._hass.callService('mqtt', 'publish', {
          topic: 'homeassistant/text/nibe_enable_entity/set',
          payload: pointId.toString()
        });
        await this.sleep(50);
        succeeded++;
      } catch (e) {
        console.warn('Failed to enable entity', pointId, e);
        // Revert optimistic update for this point on failure.
        const entity = this.entities.get(pointId);
        if (entity) entity.enabled = false;
        anyFailed = true;
      }
    }

    // Re-render if any reverts happened so the UI reflects the real state
    // rather than the stale optimistic values from the failed points.
    if (anyFailed) this.updateTable();

    const total = pointIds.length;
    if (succeeded === total) {
      this.showToast(`Enabled ${succeeded} ${succeeded === 1 ? 'entity' : 'entities'}`, 'success');
    } else if (succeeded > 0) {
      this.showToast(`Enabled ${succeeded} of ${total} — ${total - succeeded} failed`, 'error');
    } else {
      this.showToast(`Failed to enable ${total} ${total === 1 ? 'entity' : 'entities'}`, 'error');
    }
    this.clearSelection();
  }

  /**
   * Send disable commands to the bridge for a list of point IDs.
   * Mirror of enableEntities() — see that method for the sequencing and
   * optimistic update rationale.
   *
   * @param {number[]} pointIds - Array of Nibe variableId values to disable.
   */
  async disableEntities(pointIds) {
    if (!pointIds.length || !this._hass) return;

    // Optimistic update — reflect in UI immediately.
    for (const pointId of pointIds) {
      const entity = this.entities.get(pointId);
      if (entity) entity.enabled = false;
    }
    this.updateTable();

    let succeeded = 0;
    let anyFailed = false;
    for (const pointId of pointIds) {
      try {
        await this._hass.callService('mqtt', 'publish', {
          topic: 'homeassistant/text/nibe_disable_entity/set',
          payload: pointId.toString()
        });
        await this.sleep(50);
        succeeded++;
      } catch (e) {
        console.warn('Failed to disable entity', pointId, e);
        // Revert optimistic update for this point on failure.
        const entity = this.entities.get(pointId);
        if (entity) entity.enabled = true;
        anyFailed = true;
      }
    }

    if (anyFailed) this.updateTable();

    const total = pointIds.length;
    if (succeeded === total) {
      this.showToast(`Disabled ${succeeded} ${succeeded === 1 ? 'entity' : 'entities'}`, 'success');
    } else if (succeeded > 0) {
      this.showToast(`Disabled ${succeeded} of ${total} — ${total - succeeded} failed`, 'error');
    } else {
      this.showToast(`Failed to disable ${total} ${total === 1 ? 'entity' : 'entities'}`, 'error');
    }
    this.clearSelection();
  }

  /**
   * Schedule a table re-render after a short debounce delay.
   * Used by the search input to avoid re-rendering on every keystroke.
   */
  debouncedUpdate() {
    if (this.updateTimeout) clearTimeout(this.updateTimeout);
    this.updateTimeout = setTimeout(() => {
      this.updateTable();
      this.updateTimeout = null;
    }, this.debounceTime);
  }

  /**
   * Central re-render coordinator.  Applies filters, sorts, updates pagination,
   * re-renders both the desktop table and mobile cards, refreshes stat counters,
   * and updates button states.  Call this whenever filter, sort, selection, or
   * entity data changes.
   */
  updateTable() {
    if (this.isLoading && this.entities.size > 0) {
      this.isLoading = false;
    }
    
    this.filteredEntities = this.getFilteredEntities();
    this.sortEntities();
    this.updatePagination();
    this.renderTable();
    this.renderMobileCards();
    this.updateStats();
    this.updateButtonStates();
    this.updateSelectionInfo();
  }

  /**
   * Apply the current search term and filter selections to the full entity Map.
   * Returns a plain array suitable for sorting and pagination.
   * Search matches against point ID, lower-cased title, and entity type.
   *
   * @returns {Object[]} Filtered array of entity objects.
   */
  getFilteredEntities() {
    // Pre-compute Fuse results once for the whole filter pass.
    // Only runs when query is ≥ 3 chars and Fuse is loaded.
    // _fuseResultIds: Set for O(1) membership test during filter.
    // _fuseResultOrder: Map of id → rank for preserving Fuse score order.
    const term = (this.searchTerm || '').trim();
    if (term.length >= 3 && this._fuse) {
      const results = this._fuse.search(term);
      this._fuseResultIds   = new Set(results.map(r => r.item.id));
      this._fuseResultOrder = new Map(results.map((r, i) => [r.item.id, i]));
    } else {
      this._fuseResultIds   = null;
      this._fuseResultOrder = null;
    }

    return Array.from(this.entities.values()).filter(entity => {
      // Guard: skip malformed entities that have no id yet (e.g. stubs
      // added by handlePointListMessage before metadata arrives).
      if (entity.id === undefined && entity.point_id === undefined) return false;
      // Normalise: ensure id is always set for downstream code.
      if (entity.id === undefined) entity.id = entity.point_id;

      if (this.searchTerm) {
        const term      = this.searchTerm.trim();
        const termLower = term.toLowerCase();

        // Exact substring match on ID, unit, and Modbus register ID.
        // These fields are short, unambiguous, and benefit from exact matching
        // (e.g. typing "°C" or "5001" or "40004" should be precise).
        const idStr      = entity.id != null ? entity.id.toString() : '';
        const unitLower  = (entity.unit || '').toLowerCase();
        const modbus     = entity.modbusRegisterID != null
                           ? entity.modbusRegisterID.toString() : '';
        const exactMatch = idStr.includes(termLower)
                        || unitLower.includes(termLower)
                        || modbus.includes(termLower);

        if (exactMatch) return true;

        // Fuzzy match on title via Fuse.js (when loaded and query ≥ 3 chars).
        // Falls back to substring matching if Fuse is not yet available.
        const title = (entity.title || '').toLowerCase();
        if (term.length >= 3 && this._fuse) {
          // Fuse searches the full entity list — check if this entity is in results.
          if (!this._fuseResultIds) return title.includes(termLower);
          return this._fuseResultIds.has(entity.id);
        }

        // Fuse not loaded or query < 3 chars: exact substring on title.
        if (!title.includes(termLower)) return false;
      }
      
      if (this.typeFilter && entity.type !== this.typeFilter) return false;
      if (this.statusFilter === 'enabled' && !entity.enabled) return false;
      if (this.statusFilter === 'disabled' && entity.enabled) return false;
      if (this.writableFilter === 'true' && !entity.writable) return false;
      if (this.writableFilter === 'false' && entity.writable) return false;
      if (this.dynamicFilter === 'dynamic' && !entity.isDynamic) return false;
      if (this.dynamicFilter === 'static'  &&  entity.isDynamic) return false;
      
      return true;
    });
  }

  /**
   * Sort filteredEntities in-place according to the current sortField and
   * sortAscending state.  Numeric comparison for ID, boolean for enabled,
   * and case-insensitive string comparison for all other fields.
   *
   * When a Fuse fuzzy search is active (_fuseResultOrder is set), best-match
   * results are sorted to the top first; within tied Fuse ranks (e.g. exact
   * matches that also passed the unit/ID exact check) the column sort applies
   * as a tiebreaker.
   */
  sortEntities() {
    if (this._fuseResultOrder) {
      // Fuse search active — sort by match quality (score rank) first.
      // Entities that matched via exact unit/ID (not in _fuseResultOrder) get
      // rank Infinity so they appear after fuzzy title matches.
      this.filteredEntities.sort((a, b) => {
        const rankA = this._fuseResultOrder.has(a.id)
                      ? this._fuseResultOrder.get(a.id) : Infinity;
        const rankB = this._fuseResultOrder.has(b.id)
                      ? this._fuseResultOrder.get(b.id) : Infinity;
        if (rankA !== rankB) return rankA - rankB;
        // Tiebreaker: fall through to column sort for equal ranks
        return a.id - b.id;
      });
      return;
    }

    this.filteredEntities.sort((a, b) => {
      let aVal = a[this.sortField];
      let bVal = b[this.sortField];
      
      if (this.sortField === 'id') {
        aVal = parseInt(aVal);
        bVal = parseInt(bVal);
      } else if (this.sortField === 'enabled') {
        aVal = a.enabled ? 1 : 0;
        bVal = b.enabled ? 1 : 0;
      } else {
        aVal = String(aVal || '').toLowerCase();
        bVal = String(bVal || '').toLowerCase();
      }
      
      if (aVal === bVal) return 0;
      return this.sortAscending ? (aVal > bVal ? 1 : -1) : (aVal < bVal ? 1 : -1);
    });
  }

  // Escape user-supplied strings before inserting into innerHTML.
  // Entity titles, descriptions, and all metadata strings come from Nibe
  // firmware via MQTT — escaping all five HTML special characters guards
  // against unexpected content in firmware strings or crafted MQTT payloads.
  _esc(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g,  '&amp;')
      .replace(/</g,  '&lt;')
      .replace(/>/g,  '&gt;')
      .replace(/"/g,  '&quot;')
      .replace(/'/g,  '&#39;');
  }

  // Safe numeric display: only emit the value when it is a finite number,
  // otherwise fall back to the supplied default.  Prevents a non-numeric
  // value received from MQTT being inserted raw into an innerHTML template.
  _num(value, defaultValue = 'N/A') {
    return (typeof value === 'number' && isFinite(value)) ? value : defaultValue;
  }

  /**
   * Render the current page of filteredEntities into the desktop table body.
   * Replaces innerHTML entirely on each call, so attachTableEventListeners()
   * must be called immediately after to re-register row and button handlers.
   */
  renderTable() {
    const tbody = this.shadowRoot.getElementById('entity-table-body');
    if (!tbody) return;
    
    const startIndex = this.currentPage * this.config.pageSize;
    const endIndex = startIndex + this.config.pageSize;
    const pageEntities = this.filteredEntities.slice(startIndex, endIndex);
    
    if (pageEntities.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="6" class="empty">
            ${this.entities.size === 0 ? 'No entities discovered yet' : 'No entities match filters'}
          </td>
        </tr>
      `;
      return;
    }
    
    tbody.innerHTML = pageEntities.map(entity => `
      <tr data-id="${entity.id}">
        <td>
          <input type="checkbox" class="checkbox entity-checkbox" 
                ${this.selectedIds.has(entity.id) ? 'checked' : ''}
                data-id="${entity.id}">
        </td>
        <td style="font-family: monospace; font-weight: var(--ha-font-weight-bold, 600);">${entity.id}</td>
        <td>
          <div class="badge-group">
            <span class="badge badge-${entity.type}"
                  title="Entity type: ${TYPE_DISPLAY_NAMES[entity.type] || entity.type}">
              ${TYPE_DISPLAY_NAMES[entity.type] || entity.type}
            </span>
            ${entity.writable
              ? '<span class="badge badge-writable" title="Writable: this entity sends commands directly to the controller">Writable</span>'
              : '<span class="badge" style="background:#607d8b;" title="Read-only: this entity can only be monitored, not controlled">Read-only</span>'
            }
            ${entity.isDynamic
              ? '<span class="badge" style="background:#9c27b0;" title="Dynamic: this entity exists only while a related operating mode is active — it cannot be disabled manually">Dynamic</span>'
              : ''
            }
          </div>
        </td>
        <td>${this._esc(entity.title)}</td>
        <td>
          <span class="badge badge-${entity.enabled ? 'enabled' : 'disabled'}">
            ${entity.enabled ? 'Enabled' : 'Disabled'}
          </span>
        </td>
        <td>
          <div class="actions-container">
            ${entity.enabled
              ? (entity.isDynamic
                  ? `<button class="button-fixed" 
                       data-id="${entity.id}"
                       title="Dynamic entities cannot be disabled — they are controlled by the Nibe firmware"
                       disabled style="opacity:0.4;cursor:not-allowed;">Disable</button>`
                  : `<button class="button-fixed button-fixed-danger" 
                       data-action="disable" 
                       data-id="${entity.id}"
                       title="Disable this entity">Disable</button>`
                )
              : `<button class="button-fixed button-fixed-success" 
                   data-action="enable" 
                   data-id="${entity.id}"
                   title="Enable this entity">Enable</button>`
            }
            <button class="button-fixed button-fixed-secondary" 
                    data-action="details" 
                    data-id="${entity.id}"
                    title="View entity details">Details</button>
          </div>
        </td>
      </tr>
    `).join('');
    
    this.attachTableEventListeners();
  }

  /**
   * Render the current page of filteredEntities as mobile card elements.
   * Mirror of renderTable() for the mobile layout.  attachMobileEventListeners()
   * must be called immediately after to wire up checkbox and button handlers.
   */
  renderMobileCards() {
    const container = this.shadowRoot.getElementById('mobile-cards-container');
    if (!container) return;

    const startIndex = this.currentPage * this.config.pageSize;
    const endIndex = startIndex + this.config.pageSize;
    const pageEntities = this.filteredEntities.slice(startIndex, endIndex);
    
    if (pageEntities.length === 0) {
      container.innerHTML = `
        <div class="empty">
          ${this.entities.size === 0 ? 'No entities discovered yet' : 'No entities match filters'}
        </div>
      `;
      return;
    }
    
    container.innerHTML = pageEntities.map(entity => `
      <div class="entity-card" data-id="${entity.id}">
        <!-- Row 1: Checkbox + ID + Title -->
        <div class="card-row">
          <div class="card-checkbox">
            <input type="checkbox" class="checkbox-large mobile-entity-checkbox" 
                  ${this.selectedIds.has(entity.id) ? 'checked' : ''}
                  data-id="${entity.id}">
          </div>
          <span class="card-id">${entity.id}</span>
          <span class="card-title">${this._esc(entity.title)}</span>
        </div>
        
        <!-- Row 2: Badges (Type + Status + Writable + Dynamic) -->
        <div class="card-row">
          <div class="card-badges">
            <span class="badge badge-${entity.type}">${TYPE_DISPLAY_NAMES[entity.type] || entity.type.replace(/_/g, ' ')}</span>
            <span class="badge badge-${entity.enabled ? 'enabled' : 'disabled'}">${entity.enabled ? 'Enabled' : 'Disabled'}</span>
            ${entity.writable ? '<span class="badge badge-writable">Writable</span>' : '<span class="badge" style="background:#607d8b;">Read-only</span>'}
            ${entity.isDynamic ? '<span class="badge" style="background:#9c27b0;">Dynamic</span>' : ''}
          </div>
        </div>
        
        <!-- Row 3: Action Buttons -->
        <div class="card-actions">
          ${entity.enabled
            ? (entity.isDynamic
                ? `<button class="card-button"
                     data-id="${entity.id}"
                     title="Dynamic — cannot be disabled"
                     disabled style="opacity:0.4;cursor:not-allowed;">Disable</button>`
                : `<button class="card-button card-button-danger"
                     data-action="disable"
                     data-id="${entity.id}">Disable</button>`
              )
            : `<button class="card-button card-button-success"
                 data-action="enable"
                 data-id="${entity.id}">Enable</button>`
          }
          <button class="card-button card-button-secondary" 
                  data-action="details" 
                  data-id="${entity.id}">Details</button>
        </div>
      </div>
    `).join('');
    
    this.attachMobileEventListeners();
  }

  /**
   * Attach click and change handlers to the rows and buttons of the desktop table.
   * Must be called after every renderTable() call because innerHTML replacement
   * destroys previously registered listeners.
   * Clicking a row (not on a button or checkbox) opens the entity details modal.
   */
  attachTableEventListeners() {
    const tbody = this.shadowRoot.getElementById('entity-table-body');
    if (!tbody) return;
    
    tbody.querySelectorAll('.entity-checkbox').forEach(checkbox => {
      checkbox.addEventListener('change', (e) => {
        const pointId = parseInt(e.target.dataset.id);
        if (e.target.checked) {
          this.selectedIds.add(pointId);
        } else {
          this.selectedIds.delete(pointId);
        }
        this.updateButtonStates();
        this.updateSelectionInfo();
        this.renderMobileCards(); // Keep mobile view in sync
      });
    });
    
    tbody.querySelectorAll('[data-action]').forEach(button => {
      button.addEventListener('click', (e) => {
        const action = e.target.dataset.action;
        const pointId = parseInt(e.target.dataset.id);
        
        switch (action) {
          case 'enable':
            this.enableEntities([pointId]);
            break;
          case 'disable':
            this.disableEntities([pointId]);
            break;
          case 'details':
            this.showEntityDetails(pointId);
            break;
        }
      });
    });
    
    tbody.querySelectorAll('tr[data-id]').forEach(row => {
      row.addEventListener('click', (e) => {
        if (!e.target.matches('input, button') && !e.target.closest('button')) {
          const pointId = parseInt(row.dataset.id);
          this.showEntityDetails(pointId);
        }
      });
    });
  }

  /**
   * Attach handlers to the mobile card checkboxes and action buttons.
   * Also syncs the desktop table checkbox when a mobile checkbox is toggled,
   * so selection state stays consistent if the viewport is resized mid-session.
   * Must be called after every renderMobileCards() call.
   */
  attachMobileEventListeners() {
    const container = this.shadowRoot.getElementById('mobile-cards-container');
    if (!container) return;

    container.querySelectorAll('.mobile-entity-checkbox').forEach(checkbox => {
      checkbox.addEventListener('change', (e) => {
        const pointId = parseInt(e.target.dataset.id);
        if (e.target.checked) {
          this.selectedIds.add(pointId);
        } else {
          this.selectedIds.delete(pointId);
        }
        this.updateButtonStates();
        this.updateSelectionInfo();

        // Keep desktop checkbox in sync when both views are rendered
        const desktopCheckbox = this.shadowRoot.querySelector(`.entity-checkbox[data-id="${pointId}"]`);
        if (desktopCheckbox) {
          desktopCheckbox.checked = e.target.checked;
        }
      });
    });
    
    container.querySelectorAll('[data-action]').forEach(button => {
      button.addEventListener('click', (e) => {
        e.stopPropagation();
        const action = e.target.dataset.action;
        const pointId = parseInt(e.target.dataset.id);

        switch (action) {
          case 'enable':  this.enableEntities([pointId]);  break;
          case 'disable': this.disableEntities([pointId]); break;
          case 'details': this.showEntityDetails(pointId); break;
        }
      });
    });

    // Tapping anywhere on the card (not on a button or checkbox) opens details.
    container.querySelectorAll('.entity-card').forEach(card => {
      card.addEventListener('click', (e) => {
        if (!e.target.matches('input, button') && !e.target.closest('button')) {
          const pointId = parseInt(card.dataset.id);
          this.showEntityDetails(pointId);
        }
      });
    });
  }

  /** Refresh the Total and Enabled counters in the card header. */
  updateStats() {
    const total = this.entities.size;
    const enabled = Array.from(this.entities.values()).filter(e => e.enabled).length;
    
    this.setElementText('total-count', total);
    this.setElementText('enabled-count', enabled);
  }
  
  /** Show or hide the "Selected: N" stat chip based on current selection size. */
  updateSelectionInfo() {
    const selectedCount = this.selectedIds.size;
    const selectionStat = this.shadowRoot.getElementById('selection-stat');
    const clearSelectionBtn = this.shadowRoot.getElementById('clear-selection');
    const selectedCountElement = this.shadowRoot.getElementById('selected-count');
    
    if (selectedCount > 0) {
      if (selectionStat) selectionStat.style.display = 'block';
      if (selectedCountElement) selectedCountElement.textContent = selectedCount;
      if (clearSelectionBtn) clearSelectionBtn.disabled = false;
    } else {
      if (selectionStat) selectionStat.style.display = 'none';
      if (clearSelectionBtn) clearSelectionBtn.disabled = true;
    }
  }
  
  /** Refresh pagination counters and enable/disable Prev/Next buttons. */
  updatePagination() {
    const total = this.filteredEntities.length;
    const totalPages = Math.ceil(total / this.config.pageSize);
    const start = Math.min(this.currentPage * this.config.pageSize + 1, total) || 0;
    const end = Math.min(start + this.config.pageSize - 1, total) || 0;
    
    this.setElementText('total-filtered', total);
    this.setElementText('page-start', start);
    this.setElementText('page-end', end);
    
    const prevButton = this.shadowRoot.getElementById('prev-page');
    const nextButton = this.shadowRoot.getElementById('next-page');
    
    if (prevButton) prevButton.disabled = this.currentPage === 0;
    if (nextButton) nextButton.disabled = this.currentPage >= totalPages - 1;
  }

  /**
   * Enable or disable the Enable / Disable / Clear toolbar buttons based on
   * whether any entities are selected.  Also updates the select-all checkbox
   * to checked, indeterminate, or unchecked to reflect the current page state.
   */
  updateButtonStates() {
    const enableButton = this.shadowRoot.getElementById('enable-selected');
    const disableButton = this.shadowRoot.getElementById('disable-selected');
    const selectAllCheckbox = this.shadowRoot.getElementById('select-all-checkbox');
    
    const hasSelection = this.selectedIds.size > 0;
    if (enableButton) enableButton.disabled = !hasSelection;
    if (disableButton) disableButton.disabled = !hasSelection;
    
    if (selectAllCheckbox) {
      const allFilteredSelected = this.filteredEntities.length > 0 && 
        this.filteredEntities.every(e => this.selectedIds.has(e.id));
      selectAllCheckbox.checked = allFilteredSelected;
      selectAllCheckbox.indeterminate = !allFilteredSelected && 
        this.filteredEntities.some(e => this.selectedIds.has(e.id));
    }
    
    this.updateSearchClearButton();
  }

  selectAll() {
    // Snapshot the current filtered set at click time.  An MQTT update
    // between selectAll() and the subsequent enableSelected()/disableSelected()
    // call could recompute filteredEntities with new entities — without this
    // snapshot the user might enable entities they never saw selected.
    const snapshot = this.filteredEntities || [];
    snapshot.forEach(entity => {
      this.selectedIds.add(entity.id);
    });
    this.updateTable();
  }

  clearSelection() {
    this.selectedIds.clear();
    this.updateTable();
  }

  enableSelected() {
    const ids = Array.from(this.selectedIds);
    if (ids.length > 0) {
      this.enableEntities(ids);
    }
  }

  disableSelected() {
    // Dynamic entities cannot be disabled — filter them out silently.
    // The disable button is already hidden/disabled for individual dynamic
    // entities, but bulk-select could still include them if the user
    // selected-all and then clicked Disable.
    const ids = Array.from(this.selectedIds).filter(id => {
      const entity = this.entities.get(id);
      return entity && !entity.isDynamic;
    });
    if (ids.length > 0) {
      this.disableEntities(ids);
    } else if (this.selectedIds.size > 0) {
      this.showToast('Dynamic entities cannot be disabled — change the controlling register instead', 'warning');
    }
  }

  previousPage() {
    if (this.currentPage > 0) {
      this.currentPage--;
      this.updateTable();
    }
  }

  nextPage() {
    const totalPages = Math.ceil(this.filteredEntities.length / this.config.pageSize);
    if (this.currentPage < totalPages - 1) {
      this.currentPage++;
      this.updateTable();
    }
  }

  /**
   * Open the entity details modal for a given point ID.
   * Shows all Modbus metadata fields received from the bridge, plus
   * inline Enable/Disable and Refresh buttons.
   *
   * The setTimeout(10ms) delay before wiring modal button handlers works
   * around the fact that setModalContent() replaces innerHTML — without it,
   * the buttons don't yet exist in the DOM when addEventListener is called.
   *
   * @param {number} pointId - Nibe variableId of the entity to display.
   */
  showEntityDetails(pointId) {
    const entity = this.entities.get(pointId);
    if (!entity) return;
    
    const displayValue = (value, defaultValue = 'N/A') => {
      if (value === null || value === undefined || value === '') {
        return defaultValue;
      }
      // Always escape — displayValue is used in innerHTML contexts.
      return this._esc(String(value));
    };
    
    const formattedLastUpdated = this.formatDateTimeHA(new Date(entity.lastUpdated));
    
    const content = `
      <div class="entity-details">
        <div class="detail-row">
          <div class="detail-label">Point ID</div>
          <div class="detail-value" style="font-family: monospace; font-weight: bold;">${this._num(entity.id)}</div>
        </div>
        
        <div class="detail-row">
          <div class="detail-label">MODBUS Register ID</div>
          <div class="detail-value" style="font-family: monospace;">${displayValue(entity.modbusRegisterID)}</div>
        </div>
        <div class="detail-row">
          <div class="detail-label">MODBUS Register Type</div>
          <div class="detail-value">${displayValue(entity.modbusRegisterType)}</div>
        </div>
        
        <div class="detail-row">
          <div class="detail-label">Title</div>
          <div class="detail-value">${this._esc(entity.title)}</div>
        </div>
        <div class="detail-row">
          <div class="detail-label">Description</div>
          <div class="detail-value" style="font-style: italic;">${entity.description ? this._esc(entity.description) : 'No description'}</div>
        </div>
        
        <div class="detail-row">
          <div class="detail-label">Type</div>
          <div class="detail-value">
            <span class="detail-badge badge-${this._esc(entity.type)}"
                  title="Entity type: ${this._esc(TYPE_DISPLAY_NAMES[entity.type] || entity.type)}">
              ${this._esc(TYPE_DISPLAY_NAMES[entity.type] || entity.type)}
            </span>
            ${entity.writable
              ? '<span class="detail-badge badge-writable" title="Sends commands directly to the controller">Writable</span>'
              : '<span class="detail-badge" style="background:#607d8b;" title="Read-only sensor — cannot be written">Read-only</span>'
            }
            ${entity.isDynamic
              ? '<span class="detail-badge" style="background:#9c27b0;" title="Exists only while a related operating mode is active — firmware-controlled">Dynamic</span>'
              : ''
            }
          </div>
        </div>
        <div class="detail-row">
          <div class="detail-label">Status</div>
          <div class="detail-value">
            <span class="detail-badge badge-${entity.enabled ? 'enabled' : 'disabled'}"
                  title="${entity.enabled
                    ? 'Active in bridge — has a live HA entity'
                    : 'Disabled — use the table row button to enable'
                  }">
              ${entity.enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
        </div>
        
        <div class="detail-row">
          <div class="detail-label">Variable Type</div>
          <div class="detail-value">${displayValue(entity.variableType)}</div>
        </div>
        <div class="detail-row">
          <div class="detail-label">Variable Size</div>
          <div class="detail-value">${displayValue(entity.variableSize)}</div>
        </div>
        
        <div class="detail-row">
          <div class="detail-label">Unit</div>
          <div class="detail-value">
            ${displayValue(entity.unit)}
            ${entity.shortUnit ? ` (${this._esc(entity.shortUnit)})` : ''}
            ${entity.unitOverridden
              ? `<span class="detail-badge" style="background:#9c27b0;"
                       title="The bridge replaced firmware's reported unit with this value — firmware itself reported '${this._esc(entity.unitRaw || '(empty)')}'.">Overridden</span>`
              : ''
            }
          </div>
        </div>
        ${entity.unitOverridden ? `
        <div class="detail-row">
          <div class="detail-label">Unit (firmware-reported)</div>
          <div class="detail-value" style="font-style: italic; color: #888;">
            ${entity.unitRaw ? this._esc(entity.unitRaw) : '(empty)'}
          </div>
        </div>
        ` : ''}
        
        <div class="detail-row">
          <div class="detail-label">Value Range</div>
          <div class="detail-value">
            ${(typeof entity.minValue === 'number' && isFinite(entity.minValue) &&
               typeof entity.maxValue === 'number' && isFinite(entity.maxValue))
              ? `${entity.minValue} to ${entity.maxValue}`
              : 'Not specified'}
          </div>
        </div>
        <div class="detail-row">
          <div class="detail-label">Divisor</div>
          <div class="detail-value">${this._num(entity.divisor, 1) !== 1 ? this._num(entity.divisor) : '1 (no scaling)'}</div>
        </div>
        <div class="detail-row">
          <div class="detail-label">Decimal Places</div>
          <div class="detail-value">${this._num(entity.decimal, 0)}</div>
        </div>
        <div class="detail-row">
          <div class="detail-label">Change Threshold</div>
          <div class="detail-value">${displayValue(entity.change, '0 (no threshold)')}</div>
        </div>
        
        <div class="detail-row">
          <div class="detail-label">Default Values</div>
          <div class="detail-value">
            ${typeof entity.intDefaultValue === 'number' && isFinite(entity.intDefaultValue) ? `Integer: ${entity.intDefaultValue}<br>` : ''}
            ${entity.stringDefaultValue ? `String: &quot;${this._esc(entity.stringDefaultValue)}&quot;` : ''}
            ${(entity.intDefaultValue === null || !(typeof entity.intDefaultValue === 'number' && isFinite(entity.intDefaultValue))) && !entity.stringDefaultValue ? 'Not specified' : ''}
          </div>
        </div>
        
        ${entity.category ? `
        <div class="detail-row">
          <div class="detail-label">Category</div>
          <div class="detail-value">${this._esc(entity.category)}</div>
        </div>
        ` : ''}
        
        <div class="detail-row">
          <div class="detail-label">Last Updated</div>
          <div class="detail-value">${this._esc(formattedLastUpdated)}</div>
        </div>
      </div>
      
      ${entity.writable ? `
      <div style="margin-top:16px;padding:10px 12px;
           background:rgba(255,152,0,0.08);border-left:3px solid var(--warning-color,#ff9800);
           border-radius:4px;font-size:13px;color:var(--primary-text-color);">
        ⚠ Writing to this entity sends a command directly to the
        ${this.deviceModel} controller. Verify the current value before
        changing it — some registers affect heating/cooling operation
        immediately.
      </div>` : ''}
      <div style="margin-top:12px;font-size:12px;color:var(--secondary-text-color,#727272);
           font-style:italic;">
        To enable or disable this data point use the table row button.
      </div>
    `;
    
    this.setModalContent('details-modal', 'Entity Details', content);
    this.showModal('details-modal');
    
    // Enable/disable is handled via the table row button only — not from the details popup.
  }

  /**
   * Open the changelog modal, publish the mark-read command, and render content.
   *
   * The mark-read MQTT publish is fire-and-forget — unread state is cleared
   * optimistically in-memory before the broker confirms, so the unread border
   * never flashes as the modal opens.  The bridge will confirm via the
   * retained changelog/history topic shortly after.
   */
  async showChangelog() {
    if (this._hass) {
      this._hass.callService('mqtt', 'publish', {
        topic: 'homeassistant/button/nibe_mark_changes_read/press',
        payload: ''
      }).catch(e => console.warn('Failed to mark changes as read:', e));
    }

    // Clear unread state optimistically so the badge and entry borders
    // are already gone by the time the modal finishes opening.
    this.changelog.forEach(e => { e.unread = false; });
    this.unreadChanges = 0;
    this.updateChangelogBadge();
    this._renderChangelogContent();
    this.showModal('changelog-modal');
  }

  /**
   * Render (or re-render) the changelog modal content in place.
   *
   * Called by showChangelog() on open and by handleChangelogHistoryMessage()
   * when the modal is already open and fresh data arrives.  Does NOT publish
   * the mark-read command — that is the caller's responsibility.
   */
  // ── Snapshots ─────────────────────────────────────────────────────────────

  /**
   * Handle retained nibe/browser/snapshots message — update local list
   * and re-render the modal if it's open.
   */
  handleSnapshotsMessage(msg) {
    try {
      const payload = typeof msg.payload === 'string'
                      ? msg.payload
                      : new TextDecoder().decode(msg.payload);
      this.snapshots = JSON.parse(payload) || [];
    } catch (e) {
      this.snapshots = [];
    }
    // Re-render if the modal is currently open
    const modal = this.shadowRoot?.getElementById('snapshots-modal');
    if (modal && modal.style.display !== 'none') {
      this._renderSnapshotsList();
    }
  }

  handleAppliedModeMessage(msg) {
    try {
      const payload = typeof msg.payload === 'string'
                      ? msg.payload
                      : new TextDecoder().decode(msg.payload);
      this.appliedMode = payload.trim();
    } catch (e) {
      this.appliedMode = '';
    }
  }

  /** Open the snapshots modal and render current list. */
  showSnapshots() {
    this._renderSnapshotsList();
    this.showModal('snapshots-modal');
    // Focus the name input
    setTimeout(() => {
      this.shadowRoot?.getElementById('snapshot-name-input')?.focus();
    }, 50);
  }

  /** Render the snapshots list inside the modal. */
  _renderSnapshotsList() {
    const container = this.shadowRoot?.getElementById('snapshots-list');
    if (!container) return;

    // Show a warning when restore is blocked by the current mode
    const blockedModes = ['menus', 'all'];
    const modeBlocked  = blockedModes.includes(this.appliedMode);
    const warningHtml  = modeBlocked ? `
      <div style="
        margin-bottom:14px;padding:12px 14px;
        background:rgba(255,152,0,0.12);
        border-left:4px solid #ff9800;border-radius:4px;
        font-size:13px;color:var(--primary-text-color);
      ">
        ⚠️ Restore is disabled in <strong>${this._esc(this.appliedMode)}</strong> mode.
        The bridge manages the entity selection automatically in this mode —
        restoring a snapshot would conflict with it and be overwritten on restart.
        Switch to <strong>essential</strong>, <strong>monitoring</strong>,
        <strong>advanced</strong>, or <strong>none</strong> first.
      </div>` : '';

    if (this.snapshots.length === 0) {
      container.innerHTML = warningHtml + `
        <p style="color:var(--ha-color-secondary-text,#888);margin-top:8px;">
          No snapshots saved yet. Use the form above to save your current
          enabled entity selection.
        </p>`;
      return;
    }

    container.innerHTML = warningHtml + this.snapshots.map((snap, idx) => `
      <div style="
        margin-bottom:14px;padding:14px;
        background:var(--ha-color-secondary,var(--secondary-background-color));
        border-radius:6px;
      " data-snap-idx="${idx}">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;flex-wrap:wrap;">
          <div>
            <div style="font-weight:600;color:var(--primary-text-color);font-size:15px;">
              ${this._esc(snap.name)}
            </div>
            <div style="font-size:12px;color:var(--ha-color-secondary-text,#888);margin-top:3px;">
              ${snap.point_count ?? '?'} entities · saved ${this._esc(snap.timestamp || '')}
              ${snap.mode ? ` · mode: ${this._esc(snap.mode)}` : ''}
            </div>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
            <button class="button button-success snapshot-restore-btn"
              data-snap-name="${this._esc(snap.name)}"
              style="font-size:12px;padding:5px 12px;"
              ${modeBlocked ? 'disabled title="Switch mode before restoring"' : 'title="Restore this snapshot"'}
              >Restore</button>
            <button class="button button-danger snapshot-delete-btn"
              data-snap-name="${this._esc(snap.name)}"
              style="font-size:12px;padding:5px 12px;"
              title="Delete this snapshot">Delete</button>
          </div>
        </div>
        <!-- Restore mode choice — shown when Restore is clicked -->
        <div class="snapshot-restore-options" data-for="${this._esc(snap.name)}"
             style="display:none;margin-top:12px;padding-top:10px;
                    border-top:1px solid var(--divider-color,#e0e0e0);">
          <div style="font-size:13px;color:var(--primary-text-color);margin-bottom:8px;">
            How would you like to restore?
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <button class="button button-success snapshot-do-restore"
              data-snap-name="${this._esc(snap.name)}" data-mode="flush"
              style="font-size:12px;padding:5px 14px;"
              title="Disable everything currently enabled, then enable exactly the saved points">
              Replace current selection
            </button>
            <button class="button button-secondary snapshot-do-restore"
              data-snap-name="${this._esc(snap.name)}" data-mode="merge"
              style="font-size:12px;padding:5px 14px;"
              title="Keep what is currently enabled and additionally enable the saved points">
              Add to current selection
            </button>
            <button class="button snapshot-cancel-restore"
              data-snap-name="${this._esc(snap.name)}"
              style="font-size:12px;padding:5px 10px;background:transparent;
                     color:var(--ha-color-secondary-text,#888);">
              Cancel
            </button>
          </div>
          <div class="snapshot-restore-msg" data-for="${this._esc(snap.name)}"
               style="margin-top:8px;font-size:13px;
                      color:var(--ha-color-secondary-text,#888);min-height:16px;"></div>
        </div>
      </div>
    `).join('');

    // Wire Restore / Delete button clicks
    container.querySelectorAll('.snapshot-restore-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const name = btn.dataset.snapName;
        // Show the restore mode choice panel for this snapshot
        container.querySelectorAll('.snapshot-restore-options').forEach(p => {
          p.style.display = p.dataset.for === name ? 'block' : 'none';
        });
      });
    });

    container.querySelectorAll('.snapshot-cancel-restore').forEach(btn => {
      btn.addEventListener('click', () => {
        const panel = container.querySelector(
          `.snapshot-restore-options[data-for="${btn.dataset.snapName}"]`
        );
        if (panel) panel.style.display = 'none';
      });
    });

    container.querySelectorAll('.snapshot-do-restore').forEach(btn => {
      btn.addEventListener('click', () => {
        const name = btn.dataset.snapName;
        const mode = btn.dataset.mode;
        this._sendSnapshotCmd({ action: 'restore', name, mode });
        const msgEl = container.querySelector(
          `.snapshot-restore-msg[data-for="${name}"]`
        );
        if (msgEl) {
          msgEl.textContent = mode === 'flush'
            ? 'Replacing selection… changes will appear within a few seconds.'
            : 'Adding to selection… changes will appear within a few seconds.';
        }
        // Hide options panel after a moment
        setTimeout(() => {
          const panel = container.querySelector(
            `.snapshot-restore-options[data-for="${name}"]`
          );
          if (panel) panel.style.display = 'none';
        }, 3000);
      });
    });

    container.querySelectorAll('.snapshot-delete-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const name = btn.dataset.snapName;
        if (!confirm(`Delete snapshot "${name}"?`)) return;
        this._sendSnapshotCmd({ action: 'delete', name });
      });
    });
  }

  /** Handle the Save button click in the snapshots modal. */
  _handleSnapshotSave() {
    const input  = this.shadowRoot?.getElementById('snapshot-name-input');
    const msgEl  = this.shadowRoot?.getElementById('snapshot-save-msg');
    const name   = (input?.value || '').trim();

    if (!name) {
      if (msgEl) {
        msgEl.style.color = '#e53935';
        msgEl.textContent = 'Please enter a snapshot name.';
      }
      return;
    }

    this._sendSnapshotCmd({ action: 'save', name });

    if (input)  input.value = '';
    if (msgEl) {
      msgEl.style.color = 'var(--ha-color-secondary-text,#888)';
      msgEl.textContent = `Saving "${name}"…`;
      setTimeout(() => { if (msgEl) msgEl.textContent = ''; }, 4000);
    }
  }

  /**
   * Publish a snapshot command to nibe/browser/snapshots/cmd via MQTT.
   * The bridge receives this and performs the operation, then publishes
   * the updated snapshot list back to nibe/browser/snapshots.
   */
  _sendSnapshotCmd(cmd) {
    if (!this._hass) return;
    this._hass.callService('mqtt', 'publish', {
      topic:   'nibe/browser/snapshots/cmd',
      payload: JSON.stringify(cmd),
    }).catch(e => console.warn('Nibe: snapshot command failed:', e));
  }

  _renderChangelogContent() {

    if (this.changelog.length === 0) {
      this.setModalContent('changelog-modal', 'Discovery Changelog', 
        '<p style="color: var(--ha-color-secondary-text);">No changes recorded yet.</p>');
    } else {
      const content = this.changelog.map(entry => {
        const added     = Array.isArray(entry.added)   ? entry.added   : [];
        const removed   = Array.isArray(entry.removed) ? entry.removed : [];
        const timestamp = entry.timestamp || Date.now() / 1000;
        const source    = entry.source || 'firmware';
        const trig      = entry.triggered_by || null;  // {id, title} or null

        if (added.length === 0 && removed.length === 0) return '';

        const formattedTime = this.formatDateTimeHA(new Date(timestamp * 1000));

        // Visual treatment by source:
        //   firmware    — blue accent + blue 'firmware' badge
        //   learning    — green accent + green 'learned' badge
        //   ha_disabled — amber accent + amber 'HA registry' badge
        const isHaDisable      = source === 'ha_disabled';
        const isLearned        = source === 'learning';
        const isFirmwareChange = source === 'firmware_change';
        const accentColour = isHaDisable      ? '#f59e0b'
                           : isLearned        ? '#43a047'
                           : isFirmwareChange ? '#e53935'
                           : 'var(--primary-color, #03a9f4)';
        const removeColour = isHaDisable ? '#b45309' : '#db4437';
        const removeBadge  = isHaDisable ? '#f59e0b' : '#9e9e9e';

        const headerLabel = isHaDisable
          ? '🔒 Disabled via HA'
          : isFirmwareChange
          ? '⚠️ Firmware change detected'
          : isLearned
          ? `${added.length > 0 ? '🧠 Learned — ' : ''}${removed.length > 0 ? '↩️ Deactivated' : ''}`
          : `${added.length > 0 ? '🔍 Discovered ' : ''}${removed.length > 0 ? '🗑️ Removed' : ''}`;

        const sourceBadge = isHaDisable
          ? `<span style="display:inline-block;padding:2px 8px;border-radius:10px;
               font-size:11px;font-weight:600;color:white;background:#f59e0b;margin-left:8px;
             ">HA registry</span>`
          : isFirmwareChange
          ? `<span style="display:inline-block;padding:2px 8px;border-radius:10px;
               font-size:11px;font-weight:600;color:white;background:#e53935;margin-left:8px;
             ">firmware changed</span>`
          : isLearned
          ? `<span style="display:inline-block;padding:2px 8px;border-radius:10px;
               font-size:11px;font-weight:600;color:white;background:#43a047;margin-left:8px;
             ">learned</span>`
          : `<span style="display:inline-block;padding:2px 8px;border-radius:10px;
               font-size:11px;font-weight:600;color:white;background:#1e88e5;margin-left:8px;
             ">firmware</span>`;

        // triggered_by line — shown for any firmware-driven change where the
        // controlling point is known. Shows name first, id in brackets.
        const trigLine = (!isHaDisable && trig)
          ? `<div style="margin-top:6px;font-size:11px;color:var(--ha-color-secondary-text,#888);">
               ↳ Triggered by: <strong>${trig.title && trig.title !== `Point ${trig.id}` ? this._esc(trig.title) : `Point ${this._num(trig.id)}`}</strong>
               <span style="opacity:0.6;">(#${this._num(trig.id)})</span>${
                 trig.value !== undefined ? ` — value written: <strong>${this._esc(String(trig.value))}</strong>` : ''
               }
             </div>`
          : '';

        return `
        <div style="
          margin-bottom:16px; padding:12px;
          background:var(--ha-color-secondary,var(--secondary-background-color));
          border-radius:4px;
          border:1px solid var(--ha-color-outline,var(--divider-color));
          ${entry.unread ? `border-left:4px solid ${accentColour};` : ''}
        ">
          <div style="display:flex;justify-content:space-between;margin-bottom:8px;align-items:center;gap:8px;">
            <strong style="color:var(--ha-color-primary-text);min-width:0;overflow-wrap:break-word;word-break:break-word;">
              ${this._esc(headerLabel)}
              ${sourceBadge}
              ${entry.unread ? `<span style="color:${accentColour};margin-left:4px;">(New)</span>` : ''}
            </strong>
            <small style="color:var(--ha-color-secondary-text);flex-shrink:0;">${this._esc(formattedTime)}</small>
          </div>
          <div style="color:var(--ha-color-primary-text);">
            ${added.map(e => `
              <div style="margin-bottom:4px;color:#43a047;overflow-wrap:break-word;word-break:break-word;">
                ➕ #${this._num(e.id, '?')}: ${this._esc(e.title || 'Unknown')}
                <span style="display:inline-block;padding:2px 6px;margin-left:8px;
                  border-radius:10px;font-size:11px;font-weight:600;color:white;background:#0DA035;
                ">${this._esc(e.type || 'unknown')}</span>
              </div>
            `).join('')}
            ${removed.map(e => `
              <div style="margin-bottom:4px;color:${removeColour};overflow-wrap:break-word;word-break:break-word;">
                ➖ #${this._num(e.id, '?')}: ${this._esc(e.title || 'Unknown')}
                <span style="display:inline-block;padding:2px 6px;margin-left:8px;
                  border-radius:10px;font-size:11px;font-weight:600;color:white;background:${removeBadge};
                ">${this._esc(e.type || 'unknown')}</span>
              </div>
            `).join('')}
          </div>
          ${trigLine}
          ${entry.note && isFirmwareChange ? `
            <div style="margin-top:8px;padding:8px;border-radius:4px;
              background:rgba(229,57,53,0.08);font-size:11px;
              color:var(--ha-color-secondary-text,#888);
              border-left:3px solid #e53935;">
              ℹ️ ${this._esc(entry.note)}
            </div>` : ''}
        </div>
        `;
      }).filter(html => html.trim() !== '').join('');
      
      const entryCount = this.changelog.filter(e =>
        (Array.isArray(e.added) && e.added.length > 0) ||
        (Array.isArray(e.removed) && e.removed.length > 0)
      ).length;
      const cap = this.changelogCap !== null ? this.changelogCap : '—';
      const footer = `
        <p style="
          margin-top:16px; font-size:12px;
          color:var(--secondary-text-color,#727272);
          text-align:center;
          border-top:1px solid var(--divider-color,rgba(0,0,0,0.12));
          padding-top:12px;
        ">
          Showing ${entryCount} event${entryCount !== 1 ? 's' : ''}
          — history capped at ${cap} entries &nbsp;|
          <span style="display:inline-block;padding:1px 7px;border-radius:8px;
            font-size:11px;font-weight:600;color:white;background:#1e88e5;">
            firmware</span>
          <span style="display:inline-block;padding:1px 7px;border-radius:8px;
            font-size:11px;font-weight:600;color:white;background:#f59e0b;margin-left:4px;">
            HA registry</span>
        </p>`;
      this.setModalContent('changelog-modal', 'Discovery Changelog', content + footer);
    }
  }

  /**
   * Add or remove the unread-count badge on the Changelog button.
   * The badge is created as an absolutely-positioned child span so it
   * overlays the button corner without affecting its layout.
   * Caps the displayed count at 99+ to keep the badge compact.
   */
  updateChangelogBadge() {
    const button = this.shadowRoot.getElementById('show-changelog');
    if (!button) return;
    
    const oldBadge = button.querySelector('.change-badge');
    if (oldBadge) oldBadge.remove();
    
    if (this.unreadChanges > 0) {
      const badge = document.createElement('span');
      badge.className = 'change-badge';
      badge.textContent = this.unreadChanges > 99 ? '99+' : this.unreadChanges.toString();
      badge.style.cssText = `
        position: absolute;
        top: -5px;
        right: -5px;
        background: #db4437;
        color: white;
        border-radius: 10px;
        min-width: 18px;
        height: 18px;
        font-size: 10px;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 0 4px;
        font-weight: bold;
      `;
      button.style.position = 'relative';
      button.appendChild(badge);
    }
  }

  /**
   * Replace a modal's title and body content.
   * @param {string} modalId  - Shadow DOM element ID of the modal.
   * @param {string} title    - New modal title text.
   * @param {string} content  - HTML string for the modal body (innerHTML).
   */
  setModalContent(modalId, title, content) {
    const modal = this.shadowRoot.getElementById(modalId);
    if (!modal) return;
    
    const titleEl = modal.querySelector('.modal-title');
    const contentEl = modal.querySelector('.modal-body');
    
    if (titleEl) titleEl.textContent = title;
    if (contentEl) contentEl.innerHTML = content;
  }

  /**
   * Make a modal visible by adding the 'show' CSS class.
   * Tracks which modal is open in _openModalId so MQTT message handlers
   * can refresh the content in place when new data arrives.
   * @param {string} modalId - Shadow DOM element ID of the modal.
   */
  showModal(modalId) {
    const modal = this.shadowRoot.getElementById(modalId);
    if (modal) {
      modal.classList.add('show');
      this._openModalId = modalId;
    }
  }

  /**
   * Hide a modal by removing the 'show' CSS class.
   * @param {string} modalId - Shadow DOM element ID of the modal.
   */
  hideModal(modalId) {
    const modal = this.shadowRoot.getElementById(modalId);
    if (modal) {
      modal.classList.remove('show');
      if (this._openModalId === modalId) {
        this._openModalId = null;
      }
    }
  }

  /**
   * Display a temporary notification toast in the top-right corner.
   * Toasts shown while isLoading is true are suppressed by default to avoid
   * spamming the user with "enabled N entities" messages during the initial
   * state restore on page load.
   *
   * @param {string} message   - The text to display.
   * @param {string} [type]    - 'success', 'error', or 'info' (default 'info').
   * @param {number} [duration] - Visible duration in milliseconds (default 3000).
   */
  showToast(message, type = 'info', duration = 3000) {
    if (this.config.suppressInitialToasts && this.isLoading) {
      return;
    }
    
    const container = this.shadowRoot.querySelector('.toast-container');
    if (!container) return;
    
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    
    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => {
      toast.classList.remove('show');
      setTimeout(() => toast.remove(), 300);
    }, duration);
  }

  /**
   * Reset all filters, search, and sort state to their defaults and re-render.
   * Clears both the desktop and mobile filter controls so both views stay in sync.
   */
  clearFilters() {
    this.searchTerm = '';
    this.typeFilter = '';
    this.statusFilter = '';
    this.writableFilter = '';
    this.dynamicFilter = '';
    this.currentPage = 0;

    this.setElementValue('search-input', '');
    this.setElementValue('type-filter', '');
    this.setElementValue('status-filter', '');
    this.setElementValue('writable-filter', '');
    this.setElementValue('dynamic-filter', '');

    // Mirror on the mobile filter panel so it doesn't show stale values
    // if the user switches to mobile view after clearing on desktop.
    this.setElementValue('mobile-type-filter', '');
    this.setElementValue('mobile-status-filter', '');
    this.setElementValue('mobile-writable-filter', '');
    this.setElementValue('mobile-dynamic-filter', '');
    this.setElementValue('mobile-sort-filter', 'id-asc');
    
    this.updateSearchClearButton();
    this.updateTable();
  }

  /** Enable the search clear button only when there is text to clear. */
  updateSearchClearButton() {
    const searchClear = this.shadowRoot.getElementById('search-clear');
    if (searchClear) {
      searchClear.disabled = !this.searchTerm.trim();
    }
  }
  
  /**
   * Set the textContent of a shadow-DOM element by ID.
   * No-ops silently when the element is not found, which avoids the need
   * for null checks at every call site.
   */
  setElementText(id, text) {
    const element = this.shadowRoot.getElementById(id);
    if (element) element.textContent = text;
  }

  /**
   * Set the value property of a shadow-DOM form element by ID.
   * No-ops silently when the element is not found.
   */
  setElementValue(id, value) {
    const element = this.shadowRoot.getElementById(id);
    if (element) element.value = value;
  }

  /**
   * Return a Promise that resolves after the given number of milliseconds.
   * Used to add small delays between sequential MQTT publishes.
   * @param {number} ms - Delay in milliseconds.
   */
  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  /**
   * Called by the browser when the element is removed from the DOM.
   * Cleans up MQTT subscriptions and the debounce timer to prevent memory
   * leaks and stale callbacks on dashboard navigation.
   */
  disconnectedCallback() {
    this.cleanupSubscriptions();
    this.mqttSetupDone = false;
    this.eventListenersSet = false;
    
    if (this.updateTimeout) {
      clearTimeout(this.updateTimeout);
    }
  }

  /**
   * Tell HA how many grid rows the card occupies by default.
   * @returns {number} Default card height in HA grid units.
   */
  getCardSize() {
    return 4;
  }
}

customElements.define('nibe-entity-manager-card', NibeEntityManager);