/*
 * Production planning calendar: drag & drop + fetch, in the Kanban's style
 * (native HTML5 DnD, JSON POST, X-CSRFToken header).
 *
 * Every mutation ends in _refreshBoard(): fetch this same page's HTML and swap
 * in just the board and backlog fragments, rather than a full page reload, so
 * Alpine state (backlog panel, expanded ticket, open groups) survives.
 */

// Backlog panel state survives a real page reload via sessionStorage too.
const BACKLOG_OPEN_KEY = "ps-backlog-open";
const BACKLOG_SCROLL_KEY = "ps-backlog-scroll";
const BACKLOG_EXPANDED_KEY = "ps-backlog-expanded-ticket";
// Hover time before a collapsed group opens under a backlog drag, so merely
// crossing a header on the way elsewhere doesn't expand it.
const GROUP_EXPAND_DELAY_MS = 400;

function productionSchedule() {
  return {
    backlogOpen: sessionStorage.getItem(BACKLOG_OPEN_KEY) === "1",
    // One small modal covers every warning/error shape: overridable conflicts
    // ("confirm") and everything that used to be a bare alert() ("error").
    modalOpen: false,
    modalKind: "confirm", // "confirm" | "error"
    modalTitle: "",
    modalMessage: "",
    modalConflicts: [],
    modalConfirmLabel: "",
    _modalRefreshOnClose: false,
    // Drives the "drop here to return it to pending" affordance on the
    // backlog panel while a planned block is being dragged.
    isDraggingSession: false,
    unplanHover: false,
    expandedBacklogTicketId: null,
    ticketStages: [],
    // Expanded stage/machine groups, keyed by row key so a board refresh
    // doesn't collapse what the planner had opened.
    openGroups: {},

    _actionUrl: "",
    _csrfToken: "",
    _i18n: {},
    _snapMinutes: 15,
    _windowStart: "",
    _windowEnd: "",
    _canChange: false,
    _canPlan: false,
    _canUnplan: false,
    _segments: [],
    _totalMinutes: 0,
    _retry: null, // {action, payload} to resend with override:true
    _dragged: null, // session plus the row it was picked up from
    _pendingMove: null, // one locally-positioned block while its move saves
    _resizing: null, // session edge currently being resized with a pointer
    _draggedTicketId: null, // backlog card being dragged
    _draggedStage: null, // one expanded backlog stage being dragged
    _backlogItems: new Map(),
    _dropPreviewEl: null, // snapped drop-target box shown on the hovered track
    _dropPreviewTrack: null,
    _groupExpandKey: null, // collapsed group waiting to open under a drag
    _groupExpandTimer: null,

    init() {
      const root = document.getElementById("ps-root");
      this._actionUrl = root.dataset.actionUrl;
      this._csrfToken = root.dataset.csrfToken;
      this._snapMinutes = parseInt(root.dataset.snapMinutes || "15", 10);
      this._windowStart = root.dataset.windowStart;
      this._windowEnd = root.dataset.windowEnd;
      this._canChange = root.dataset.canChange === "1";
      this._canPlan = root.dataset.canPlan === "1";
      this._canUnplan = root.dataset.canUnplan === "1";

      const segmentsEl = document.getElementById("ps-segments");
      this._segments = segmentsEl ? JSON.parse(segmentsEl.textContent) : [];
      this._totalMinutes = this._segments.reduce((sum, s) => sum + s.minutes, 0);

      const backlogEl = document.getElementById("ps-backlog-data");
      const backlogItems = backlogEl ? JSON.parse(backlogEl.textContent) : [];
      this._backlogItems = new Map(
        backlogItems.map((item) => [String(item.ticket_id), item])
      );

      // Rendered server-side; see ProductionScheduleView._js_strings for why.
      const i18nEl = document.getElementById("ps-i18n");
      this._i18n = i18nEl ? JSON.parse(i18nEl.textContent) : {};

      this._bindDragEvents();
      this._bindResizeEvents();
      this._bindBacklogScrollTracking();
      this._tickNow();
      setInterval(() => this._tickNow(), 60000);

      this.$watch("backlogOpen", (open) => {
        sessionStorage.setItem(BACKLOG_OPEN_KEY, open ? "1" : "0");
        if (open) this.$nextTick(() => this._restoreBacklogScroll());
      });
      if (this.backlogOpen) {
        this.$nextTick(() => this._restoreBacklogScroll());
      }

      this.$watch("expandedBacklogTicketId", (ticketId) => {
        if (ticketId === null || ticketId === undefined) {
          sessionStorage.removeItem(BACKLOG_EXPANDED_KEY);
        } else {
          sessionStorage.setItem(BACKLOG_EXPANDED_KEY, String(ticketId));
        }
      });
      this._restoreExpandedBacklogTicket();
    },

    _restoreExpandedBacklogTicket() {
      const saved = sessionStorage.getItem(BACKLOG_EXPANDED_KEY);
      if (!saved) return;
      const ticketId = Number(saved);
      // May have been fully planned (and dropped from the backlog) since.
      if (!this._prepareTicketStages(ticketId)) {
        sessionStorage.removeItem(BACKLOG_EXPANDED_KEY);
        return;
      }
      this.expandedBacklogTicketId = ticketId;
    },

    isGroupOpen(key) {
      return Boolean(this.openGroups[key]);
    },

    toggleGroup(key) {
      this.openGroups[key] = !this.openGroups[key];
    },

    // --- backlog panel persistence ------------------------------------

    _bindBacklogScrollTracking() {
      const scroller = document.querySelector(".ps-backlog-scroll");
      if (!scroller) return;
      scroller.addEventListener(
        "scroll",
        () => sessionStorage.setItem(BACKLOG_SCROLL_KEY, String(scroller.scrollLeft)),
        { passive: true }
      );
    },

    _restoreBacklogScroll() {
      const scroller = document.querySelector(".ps-backlog-scroll");
      const saved = sessionStorage.getItem(BACKLOG_SCROLL_KEY);
      if (scroller && saved !== null) scroller.scrollLeft = Number(saved) || 0;
    },

    // --- axis: mirrors production/services/schedule_geometry.py -----------
    // Only proposes a snapped datetime while dragging; the server recomputes
    // the authoritative geometry, so drift here is never a wrong save.

    _offsetForDate(iso) {
      const moment = new Date(iso);
      if (!this._segments.length) return 0;
      const first = new Date(this._segments[0].start);
      const last = new Date(this._segments[this._segments.length - 1].end);
      if (moment <= first) return 0;
      if (moment >= last) return this._totalMinutes;

      for (const segment of this._segments) {
        const start = new Date(segment.start);
        const end = new Date(segment.end);
        if (moment < start) return segment.offset;
        if (moment < end) {
          return segment.offset + Math.floor((moment - start) / 60000);
        }
      }
      return this._totalMinutes;
    },

    _dateForOffset(minutes) {
      let remaining = Math.max(0, Math.min(minutes, this._totalMinutes));
      for (const segment of this._segments) {
        if (remaining < segment.minutes) {
          return new Date(new Date(segment.start).getTime() + remaining * 60000);
        }
        remaining -= segment.minutes;
      }
      const lastSegment = this._segments[this._segments.length - 1];
      return lastSegment ? new Date(lastSegment.end) : new Date(minutes * 60000);
    },

    _pctToDate(pct) {
      const offset = (pct / 100) * this._totalMinutes;
      const snapped = Math.floor(offset / this._snapMinutes) * this._snapMinutes;
      return this._dateForOffset(snapped);
    },

    // --- drag & drop --------------------------------------------------

    _bindDragEvents() {
      document.addEventListener("dragstart", (event) => this._onDragStart(event));
      document.addEventListener("dragend", (event) => this._onDragEnd(event));
      document.addEventListener("dragover", (event) => this._onDragOver(event));
      document.addEventListener("dragleave", (event) => this._onDragLeave(event));
      document.addEventListener("drop", (event) => this._onDrop(event));
    },

    _bindResizeEvents() {
      document.addEventListener("pointerdown", (event) => this._onResizeStart(event), true);
      document.addEventListener("pointermove", (event) => this._onResizeMove(event), true);
      document.addEventListener("pointerup", (event) => this._onResizeEnd(event), true);
      document.addEventListener("pointercancel", (event) => this._onResizeEnd(event), true);
    },

    _onResizeStart(event) {
      const handle = event.target.closest(".ps-block__resize");
      if (!handle || !this._canChange) return;

      const block = handle.closest(".ps-block");
      const track = block && block.closest(".ps-track");
      if (!block || !track || block.dataset.locked === "1") return;

      event.preventDefault();
      event.stopPropagation();
      handle.setPointerCapture?.(event.pointerId);

      this._resizing = {
        sessionId: block.dataset.sessionId,
        edge: handle.dataset.resizeEdge,
        block,
        handle,
        originalDraggable: block.getAttribute("draggable"),
        start: new Date(block.dataset.start),
        end: new Date(block.dataset.end),
        previewStart: new Date(block.dataset.start),
        previewEnd: new Date(block.dataset.end),
        track,
      };
      block.setAttribute("draggable", "false");
      block.classList.add("ps-block--resizing");
      this._highlightLinked(block.dataset.sessionId, true);
    },

    _onResizeMove(event) {
      const resize = this._resizing;
      if (!resize) return;

      event.preventDefault();
      const rect = resize.track.getBoundingClientRect();
      const pct = Math.max(0, Math.min(100, (event.clientX - rect.left) / rect.width * 100));
      const proposed = this._pctToDate(pct);
      const minimum = this._snapMinutes * 60 * 1000;

      if (resize.edge === "start") {
        resize.previewStart = new Date(
          Math.min(proposed.getTime(), resize.end.getTime() - minimum)
        );
        resize.previewEnd = resize.end;
      } else {
        resize.previewStart = resize.start;
        resize.previewEnd = new Date(
          Math.max(proposed.getTime(), resize.start.getTime() + minimum)
        );
      }
      this._previewResizedBlock(
        resize.block, resize.previewStart, resize.previewEnd
      );
    },

    _onResizeEnd(event) {
      const resize = this._resizing;
      if (!resize) return;

      if (resize.handle.hasPointerCapture?.(event.pointerId)) {
        resize.handle.releasePointerCapture(event.pointerId);
      }
      resize.block.setAttribute("draggable", resize.originalDraggable || "false");
      resize.block.classList.remove("ps-block--resizing");
      this._highlightLinked(null, false);
      this._resizing = null;

      const changed = resize.previewStart.getTime() !== resize.start.getTime() ||
        resize.previewEnd.getTime() !== resize.end.getTime();
      if (!changed) return;

      this._post(
        "resize_session",
        {
          session_id: resize.sessionId,
          start: resize.edge === "start" ? resize.previewStart.toISOString() : null,
          end: resize.edge === "end" ? resize.previewEnd.toISOString() : null,
        }
      );
    },

    _previewResizedBlock(block, start, end) {
      const left = this._offsetForDate(start.toISOString()) / this._totalMinutes * 100;
      const right = this._offsetForDate(end.toISOString()) / this._totalMinutes * 100;
      block.style.left = left + "%";
      block.style.width = Math.max(0, right - left) + "%";
    },

    _onDragStart(event) {
      if (this._resizing || event.target.closest(".ps-block__resize, .ps-block__session-link")) {
        event.preventDefault();
        return;
      }
      const block = event.target.closest(".ps-block");
      if (block && block.getAttribute("draggable") === "true") {
        const row = block.closest(".ps-row");
        // A block in a resourceless row (stage header, "no operator yet") can
        // still move in time; dragging it onto a real row assigns that resource.
        if (!row) {
          event.preventDefault();
          return;
        }

        const rect = block.getBoundingClientRect();
        this._dragged = {
          sessionId: block.dataset.sessionId,
          block,
          ticketId: block.dataset.ticketId || null,
          resourceIds: (block.dataset.resourceIds || "")
            .split(",")
            .filter(Boolean)
            .map(Number),
          grabOffsetPx: event.clientX - rect.left,
          widthPct: parseFloat(block.style.width) || 0,
          stage: block.dataset.stage || "",
          originResourceId: row && row.dataset.rowResourceId
            ? parseInt(row.dataset.rowResourceId, 10)
            : null,
          originParentMachineId: row && row.dataset.parentMachineId
            ? parseInt(row.dataset.parentMachineId, 10)
            : null,
          originKind: row ? row.dataset.rowKind : null,
        };
        block.classList.add("ps-block--dragging");
        this.isDraggingSession = true;
        this._highlightLinked(block.dataset.sessionId, true);
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", block.dataset.sessionId);
        return;
      }

      const card = event.target.closest(".ps-backlog-card");
      if (card && card.getAttribute("draggable") === "true") {
        this._draggedTicketId = card.dataset.ticketId;
        card.classList.add("ps-backlog-card--dragging");
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("text/plain", card.dataset.ticketId);
        return;
      }

      const stageCard = event.target.closest(".ps-backlog-stage");
      if (stageCard && stageCard.getAttribute("draggable") === "true") {
        const stage = this.ticketStages.find(
          (item) => item.stage === stageCard.dataset.stageName
        );
        if (!stage) return;
        this._draggedStage = {
          ticketId: stageCard.dataset.ticketId,
          stageName: stageCard.dataset.stageName,
          compatibleResourceIds: stage.compatibleResourceIds,
          machineQty: stage.machineQty,
          employeeQty: stage.employeeQty,
        };
        stageCard.classList.add("ps-backlog-card--dragging");
        this._highlightStageDropTargets(this._draggedStage);
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("text/plain", stageCard.dataset.stageName);
      }
    },

    _onDragEnd(event) {
      document
        .querySelectorAll(".ps-block--dragging, .ps-backlog-card--dragging")
        .forEach((el) => el.classList.remove("ps-block--dragging", "ps-backlog-card--dragging"));
      this._highlightLinked(null, false);
      document.querySelectorAll(".ps-track--dragover").forEach((el) => {
        el.classList.remove("ps-track--dragover");
      });
      document.querySelectorAll(".ps-row--stage-available").forEach((row) => {
        row.classList.remove("ps-row--stage-available");
      });
      this._clearDropPreview();
      this._cancelGroupExpand();
      this._dragged = null;
      this._draggedTicketId = null;
      this._draggedStage = null;
      this.isDraggingSession = false;
      this.unplanHover = false;
    },

    // The backlog panel only accepts a planned block, and only from someone
    // allowed to delete one -- it is a real delete, not a soft unschedule.
    _unplanZoneFor(event) {
      if (!this._dragged || !this._canUnplan) return null;
      return event.target.closest("#ps-backlog-section");
    },

    _confirmUnplan(dragged) {
      this._openModal({
        kind: "confirm",
        title: this._i18n.unplanTitle,
        message: this._i18n.unplanMessage,
        retry: { action: "unplan_session", payload: { session_id: dragged.sessionId } },
        confirmLabel: this._i18n.unplanTitle,
      });
    },

    _onDragOver(event) {
      // The backlog panel is where a planned block goes to be unplanned. It
      // holds no track, so this has to be answered before the track lookup.
      if (this._unplanZoneFor(event)) {
        event.preventDefault();
        this.unplanHover = true;
        this._clearDropPreview();
        return;
      }

      this._scheduleGroupExpand(event);

      const track = event.target.closest(".ps-track");
      if (!track || (!this._dragged && !this._draggedTicketId && !this._draggedStage)) {
        this._clearDropPreview();
        return;
      }
      if (this._draggedStage && !this._stageCanDropOnRow(
        this._draggedStage, track.closest(".ps-row")
      )) {
        this._clearDropPreview();
        return;
      }
      if (this._dragged && !this._sessionCanDropOnRow(
        this._dragged, track.closest(".ps-row")
      )) {
        this._clearDropPreview();
        return;
      }
      event.preventDefault();
      track.classList.add("ps-track--dragover");
      this._updateDropPreview(event, track);
    },

    _onDragLeave(event) {
      const panel = event.target.closest("#ps-backlog-section");
      if (panel && !panel.contains(event.relatedTarget)) this.unplanHover = false;

      const track = event.target.closest(".ps-track");
      if (track && !track.contains(event.relatedTarget)) {
        track.classList.remove("ps-track--dragover");
        if (this._dropPreviewTrack === track) this._clearDropPreview();
      }
    },

    _onDrop(event) {
      if (this._unplanZoneFor(event)) {
        event.preventDefault();
        this.unplanHover = false;
        this._confirmUnplan(this._dragged);
        return;
      }

      const track = event.target.closest(".ps-track");
      if (!track) return;
      if (this._draggedStage && !this._stageCanDropOnRow(
        this._draggedStage, track.closest(".ps-row")
      )) {
        return;
      }
      if (this._dragged && !this._sessionCanDropOnRow(
        this._dragged, track.closest(".ps-row")
      )) {
        return;
      }
      event.preventDefault();
      track.classList.remove("ps-track--dragover");
      this._clearDropPreview();

      if (this._dragged) {
        this._dropSession(event, track);
      } else if (this._draggedTicketId) {
        this._dropBacklogTicket(event, track);
      } else if (this._draggedStage) {
        this._dropBacklogStage(event, track);
      }
    },

    _dropPct(event, track, grabOffsetPx) {
      const rect = track.getBoundingClientRect();
      const leftPx = event.clientX - (grabOffsetPx || 0) - rect.left;
      return Math.max(0, Math.min(100, (leftPx / rect.width) * 100));
    },

    // Snapped preview of where the drag would land, shown on the hovered track.
    _updateDropPreview(event, track) {
      const grabOffsetPx = this._dragged ? this._dragged.grabOffsetPx : 0;
      const rawPct = this._dropPct(event, track, grabOffsetPx);
      const snapped = this._pctToDate(rawPct);
      const snappedPct = this._offsetForDate(snapped.toISOString()) / this._totalMinutes * 100;
      const widthPct = this._dragged ? this._dragged.widthPct : this._durationPctForActiveDrag();

      if (this._dropPreviewTrack !== track) {
        this._clearDropPreview();
        this._dropPreviewEl = document.createElement("div");
        this._dropPreviewEl.className = "ps-drop-preview";
        track.appendChild(this._dropPreviewEl);
        this._dropPreviewTrack = track;
      }
      this._dropPreviewEl.classList.toggle("ps-drop-preview--marker", widthPct == null);
      this._dropPreviewEl.style.left = snappedPct + "%";
      this._dropPreviewEl.style.width = widthPct != null ? Math.max(widthPct, 0.4) + "%" : "2px";
    },

    // Backlog drags don't carry a block width to mirror, so fall back to the
    // stage's own configured duration when one is already known.
    _durationPctForActiveDrag() {
      let minutes = null;
      if (this._draggedStage) {
        const stage = this.ticketStages.find(
          (item) => item.stage === this._draggedStage.stageName
        );
        minutes = stage ? Number(stage.durationMinutes) : null;
      } else if (this._draggedTicketId) {
        const item = this._backlogItems.get(String(this._draggedTicketId));
        const stage = item && item.stages ? item.stages.find((s) => !s.is_planned) : null;
        minutes = stage ? Number(stage.total_time_min) : null;
      }
      return Number.isFinite(minutes) && minutes > 0 ? (minutes / this._totalMinutes) * 100 : null;
    },

    _clearDropPreview() {
      if (this._dropPreviewEl) this._dropPreviewEl.remove();
      this._dropPreviewEl = null;
      this._dropPreviewTrack = null;
    },

    _dropSession(event, track) {
      const dragged = this._dragged;
      const row = track.closest(".ps-row");
      if (!row) return;

      const start = this._pctToDate(this._dropPct(event, track, dragged.grabOffsetPx));

      if (row.dataset.rowTicketId !== undefined) {
        this._reorderWithinTicketRow(dragged, row, event);
        return;
      }

      if (row.dataset.rowResourceId === undefined) {
        // The stage header and the "no operator yet" row stand for no resource
        // of their own, so landing on one can only ever be a time change.
        this._previewDroppedSession(dragged, track, start);
        this._post(
          "move_session",
          { session_id: dragged.sessionId, start: start.toISOString(), resource_ids: null }
        );
        return;
      }

      const targetResourceId = parseInt(row.dataset.rowResourceId, 10);
      let resourceIds = null;
      const targetParentMachineId = row.dataset.parentMachineId
        ? parseInt(row.dataset.parentMachineId, 10)
        : null;
      const sameRow = dragged.originResourceId === targetResourceId &&
        dragged.originParentMachineId === targetParentMachineId;

      if (!sameRow) {
        resourceIds = dragged.resourceIds.slice();

        // Swap like for like. In the nested view this means operator for
        // operator while the machine remains attached.
        if (dragged.originKind === row.dataset.rowKind && dragged.originResourceId) {
          resourceIds = resourceIds.filter((id) => id !== dragged.originResourceId);
        }
        if (!resourceIds.includes(targetResourceId)) resourceIds.push(targetResourceId);

        // Moving between nested machine groups changes the machine as well as
        // the operator represented by the child row.
        if (targetParentMachineId) {
          if (dragged.originParentMachineId &&
              dragged.originParentMachineId !== targetParentMachineId) {
            resourceIds = resourceIds.filter(
              (id) => id !== dragged.originParentMachineId
            );
          }
          if (!resourceIds.includes(targetParentMachineId)) {
            resourceIds.push(targetParentMachineId);
          }
        }
      }

      this._previewDroppedSession(dragged, track, start);
      this._post(
        "move_session",
        {
          session_id: dragged.sessionId,
          start: start.toISOString(),
          resource_ids: resourceIds,
        }
      );
    },

    _previewDroppedSession(dragged, track, start) {
      const block = dragged.block;
      if (!block) return;

      this._pendingMove = {
        block,
        originTrack: block.parentElement,
        originNextSibling: block.nextSibling,
        originalLeft: block.style.left,
        originalWidth: block.style.width,
        originalStart: block.dataset.start,
        originalDraggable: block.getAttribute("draggable"),
      };

      const left = this._offsetForDate(start.toISOString()) / this._totalMinutes * 100;
      block.setAttribute("draggable", "false");
      block.dataset.start = start.toISOString();
      block.style.left = left + "%";
      track.appendChild(block);
    },

    _restorePendingMove() {
      const move = this._pendingMove;
      if (!move) return;

      move.block.style.left = move.originalLeft;
      move.block.style.width = move.originalWidth;
      move.block.dataset.start = move.originalStart;
      move.block.setAttribute("draggable", move.originalDraggable || "false");
      if (move.originTrack) {
        move.originTrack.insertBefore(move.block, move.originNextSibling);
      }
      this._pendingMove = null;
    },

    _reorderWithinTicketRow(dragged, row, event) {
      const blocks = Array.from(row.querySelectorAll(".ps-block")).filter(
        (b) => b.dataset.sessionId !== dragged.sessionId
      );
      if (!blocks.length) return;

      const dropX = event.clientX;
      let insertAt = blocks.length;
      for (let i = 0; i < blocks.length; i++) {
        const rect = blocks[i].getBoundingClientRect();
        if (dropX < rect.left + rect.width / 2) {
          insertAt = i;
          break;
        }
      }

      const before = blocks[insertAt];
      const after = before ? null : blocks[blocks.length - 1];

      this._post(
        "reorder_ticket",
        {
          ticket_id: row.dataset.rowTicketId,
          session_id: dragged.sessionId,
          before_session_id: before ? before.dataset.sessionId : null,
          after_session_id: after ? after.dataset.sessionId : null,
          cascade: true,
        }
      );
    },

    _dropBacklogTicket(event, track) {
      const row = track.closest(".ps-row");
      const ticketId = this._draggedTicketId;
      if (!row || row.dataset.rowResourceId === undefined || !this._canPlan) return;

      const start = this._pctToDate(this._dropPct(event, track, 0));
      const preferredResourceIds = this._stageDropResourceIds(row);

      const backlogItem = this._backlogItems.get(String(ticketId));
      if (backlogItem && (
        backlogItem.needs_time_configuration || backlogItem.planned_stage_count > 0
      )) {
        this.openTicketStages(ticketId);
        return;
      }

      this._post(
        "plan_ticket",
        {
          ticket_id: ticketId,
          start: start.toISOString(),
          anchor_resource_id: parseInt(row.dataset.rowResourceId, 10),
          preferred_resource_ids: preferredResourceIds,
        }
      );
    },

    _dropBacklogStage(event, track) {
      const row = track.closest(".ps-row");
      const dragged = this._draggedStage;
      if (!row || !dragged || !this._canPlan || !this._stageCanDropOnRow(dragged, row)) {
        return;
      }

      const start = this._pctToDate(this._dropPct(event, track, 0));
      const resourceIds = this._stageDropResourceIds(row);

      const isCurrentTicket = this.expandedBacklogTicketId === Number(dragged.ticketId) &&
        this.ticketStages.length > 0;
      if (!isCurrentTicket && !this._prepareTicketStages(dragged.ticketId)) return;

      this.backlogOpen = true;
      this.expandedBacklogTicketId = Number(dragged.ticketId);
      const stage = this.ticketStages.find((item) => item.stage === dragged.stageName);
      if (!stage || stage.isPlanned) return;

      const duration = Number(stage.durationMinutes);
      if (!Number.isInteger(duration) || duration <= 0) {
        this._openModal({
          kind: "error",
          title: this._i18n.missingInfoTitle,
          message: this._i18n.missingDurationMessage,
        });
        return;
      }

      this._post(
        "plan_ticket_stage_custom",
        {
          ticket_id: Number(dragged.ticketId),
          start: start.toISOString(),
          stage: {
            stage: stage.stage,
            duration_minutes: duration,
            resource_ids: resourceIds,
          },
        }
      );
    },

    // --- ticket-specific stage planning --------------------------------

    _prepareTicketStages(ticketId) {
      if (!this._canPlan) return;
      const item = this._backlogItems.get(String(ticketId));
      if (!item) return;

      this.ticketStages = item.stages.map((source) => ({
        stage: source.stage,
        durationMinutes: source.total_time_min > 0 ? source.total_time_min : "",
        isPlanned: Boolean(source.is_planned),
        compatibleResourceIds: source.compatible_resource_ids || [],
        machineQty: source.machine_qty || 0,
        employeeQty: source.employee_qty || 0,
      }));
      return true;
    },

    _stageDropResourceIds(row) {
      if (!row || row.dataset.rowResourceId === undefined) return [];
      return [row.dataset.parentMachineId, row.dataset.rowResourceId]
        .filter(Boolean)
        .map(Number);
    },

    // Normalise before comparing, or an accented name like ALMACÉN can miss itself.
    _sameStage(a, b) {
      return (a || "").normalize("NFC").trim() === (b || "").normalize("NFC").trim();
    },

    // A session can only land on a row of its own stage -- refused outright,
    // not raised as an overridable warning.
    _sessionCanDropOnRow(dragged, row) {
      if (!dragged || !row) return false;
      if (!row.dataset.stageName) return true; // rows that span every stage
      return this._sameStage(row.dataset.stageName, dragged.stage);
    },

    _stageCanDropOnRow(stage, row) {
      if (!stage || !row || row.dataset.rowResourceId === undefined) return false;
      if (
        row.dataset.stageName &&
        !this._sameStage(row.dataset.stageName, stage.stageName)
      ) {
        return false;
      }
      const compatible = new Set(stage.compatibleResourceIds.map(Number));
      const resourceId = Number(row.dataset.rowResourceId);
      const parentMachineId = Number(row.dataset.parentMachineId);
      const isMachineRow = row.dataset.rowKind === "machine";
      const isEmployeeRow = row.dataset.rowKind === "employee";
      const hasResource = compatible.has(resourceId);
      const hasParentMachine = Boolean(parentMachineId) && compatible.has(parentMachineId);

      if (stage.machineQty > 1 || stage.employeeQty > 1) return false;
      if (stage.machineQty && stage.employeeQty) {
        return isEmployeeRow && hasResource && hasParentMachine;
      }
      if (stage.machineQty) return isMachineRow && hasResource;
      if (stage.employeeQty) return isEmployeeRow && hasResource;
      return hasResource;
    },

    _highlightStageDropTargets(stage) {
      document.querySelectorAll(".ps-row").forEach((row) => {
        if (this._stageCanDropOnRow(stage, row)) {
          row.classList.add("ps-row--stage-available");
        }
      });
    },

    // --- expanding collapsed groups under a backlog drag -----------------

    // The stages a backlog drag would plan: the dragged stage itself, or every
    // stage a whole ticket still has pending (plan_ticket lays them all out).
    _backlogDragStages() {
      if (this._draggedStage) return [this._draggedStage];
      if (!this._draggedTicketId) return [];
      const item = this._backlogItems.get(String(this._draggedTicketId));
      if (!item || !item.stages) return [];
      return item.stages
        .filter((source) => !source.is_planned)
        .map((source) => ({
          stageName: source.stage,
          compatibleResourceIds: source.compatible_resource_ids || [],
          machineQty: source.machine_qty || 0,
          employeeQty: source.employee_qty || 0,
        }));
    },

    // Only groups hiding a row one of the dragged stages can land on are opened.
    _groupHasDropTarget(headerRow, stages) {
      const content = headerRow.nextElementSibling;
      if (!content) return false;
      const rows = Array.from(content.querySelectorAll(".ps-row"));
      return stages.some(
        (stage) => rows.some((row) => this._stageCanDropOnRow(stage, row))
      );
    },

    _scheduleGroupExpand(event) {
      const headerRow = event.target.closest(".ps-row[data-group-key]");
      const key = headerRow ? headerRow.dataset.groupKey : null;
      if (key !== null && key === this._groupExpandKey) return;
      this._cancelGroupExpand();
      if (key === null || this.isGroupOpen(key)) return;

      const stages = this._backlogDragStages();
      if (!stages.length || !this._groupHasDropTarget(headerRow, stages)) return;

      this._groupExpandKey = key;
      this._groupExpandTimer = setTimeout(() => {
        this.openGroups[key] = true;
        this._groupExpandKey = null;
        this._groupExpandTimer = null;
      }, GROUP_EXPAND_DELAY_MS);
    },

    _cancelGroupExpand() {
      clearTimeout(this._groupExpandTimer);
      this._groupExpandTimer = null;
      this._groupExpandKey = null;
    },

    toggleTicketStages(ticketId) {
      if (!this._canPlan) return;
      if (this.expandedBacklogTicketId === Number(ticketId)) {
        this.expandedBacklogTicketId = null;
        this.ticketStages = [];
        return;
      }

      if (!this._prepareTicketStages(ticketId)) return;
      this.backlogOpen = true;
      this.expandedBacklogTicketId = Number(ticketId);
    },

    openTicketStages(ticketId) {
      if (!this._prepareTicketStages(ticketId)) return;
      this.backlogOpen = true;
      this.expandedBacklogTicketId = Number(ticketId);
    },

    _highlightLinked(sessionId, on) {
      if (!sessionId) {
        document.querySelectorAll(".ps-block--linked").forEach((el) => {
          el.classList.remove("ps-block--linked");
        });
        return;
      }
      document.querySelectorAll(`[data-session-id="${sessionId}"]`).forEach((el) => {
        el.classList.toggle("ps-block--linked", on);
      });
    },

    // --- network --------------------------------------------------------

    async _post(action, payload) {
      const body = Object.assign(
        {
          action,
          override: false,
          window_start: this._windowStart,
          window_end: this._windowEnd,
        },
        payload
      );

      let response;
      try {
        response = await fetch(this._actionUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": this._csrfToken,
          },
          body: JSON.stringify(body),
        });
      } catch (error) {
        this._restorePendingMove();
        this._openModal({
          kind: "error",
          title: this._i18n.networkErrorTitle,
          message: this._i18n.networkErrorMessage,
          refreshOnClose: true,
        });
        return;
      }

      let result;
      try {
        result = await response.json();
      } catch (error) {
        this._pendingMove = null;
        this._refreshBoard();
        return;
      }

      if (result.requires_confirmation) {
        this._openModal({
          kind: "confirm",
          title: this._i18n.hasWarningsTitle,
          conflicts: result.conflicts || [],
          retry: { action, payload: Object.assign({}, payload, { override: true }) },
          refreshOnClose: true,
        });
        return;
      }

      if (!result.ok) {
        this._restorePendingMove();
        this._openModal({
          kind: "error",
          title: this._i18n.couldNotApplyTitle,
          message: result.conflicts?.length ? "" : (result.error || this._i18n.couldNotApplyTitle),
          conflicts: result.conflicts || [],
          refreshOnClose: true,
        });
        return;
      }

      this._pendingMove = null;
      await this._refreshBoard();
    },

    // Fetches this page's HTML and swaps in just the board/backlog fragments,
    // trusting the server's render without tearing down Alpine state.
    async _refreshBoard() {
      let html;
      try {
        const response = await fetch(window.location.href);
        html = await response.text();
      } catch (error) {
        window.location.reload(); // the fetch itself failed; a real reload is the honest fallback
        return;
      }

      const doc = new DOMParser().parseFromString(html, "text/html");

      const scroller = document.querySelector(".ps-backlog-scroll");
      const savedScrollLeft = scroller ? scroller.scrollLeft : null;
      const board = document.querySelector(".ps-scroll");
      const savedBoardScroll = board
        ? { left: board.scrollLeft, top: board.scrollTop }
        : null;

      this._swapFragment("#ps-board", doc);
      this._swapFragment("#ps-backlog-section", doc);
      this._swapFragment("#ps-backlog-count", doc);

      const segmentsEl = doc.getElementById("ps-segments");
      this._segments = segmentsEl ? JSON.parse(segmentsEl.textContent) : [];
      this._totalMinutes = this._segments.reduce((sum, s) => sum + s.minutes, 0);

      const backlogEl = doc.getElementById("ps-backlog-data");
      const backlogItems = backlogEl ? JSON.parse(backlogEl.textContent) : [];
      this._backlogItems = new Map(
        backlogItems.map((item) => [String(item.ticket_id), item])
      );

      const newBoard = document.querySelector(".ps-scroll");
      if (newBoard && savedBoardScroll) {
        newBoard.scrollLeft = savedBoardScroll.left;
        newBoard.scrollTop = savedBoardScroll.top;
      }

      this._bindBacklogScrollTracking();
      const newScroller = document.querySelector(".ps-backlog-scroll");
      if (newScroller && savedScrollLeft != null) {
        newScroller.scrollLeft = savedScrollLeft;
      } else {
        this._restoreBacklogScroll();
      }

      this._refreshExpandedBacklogTicket();
      this._tickNow();
    },

    // Swapped-in content needs Alpine.initTree() or its bindings stay inert.
    _swapFragment(selector, doc) {
      const current = document.querySelector(selector);
      const next = doc.querySelector(selector);
      if (!current || !next) return;
      current.innerHTML = next.innerHTML;
      window.Alpine.initTree(current);
    },

    // Re-derive the expanded ticket's stage list, which can go stale on refresh.
    _refreshExpandedBacklogTicket() {
      if (this.expandedBacklogTicketId == null) return;
      if (!this._prepareTicketStages(this.expandedBacklogTicketId)) {
        this.expandedBacklogTicketId = null;
        this.ticketStages = [];
      }
    },

    _openModal({ kind, title, message = "", conflicts = [], retry = null, refreshOnClose = false, confirmLabel = "" }) {
      this.modalKind = kind;
      this.modalTitle = title;
      this.modalMessage = message;
      this.modalConflicts = conflicts;
      this.modalConfirmLabel = confirmLabel;
      this._retry = retry;
      this._modalRefreshOnClose = refreshOnClose;
      this.modalOpen = true;
    },

    closeModal() {
      const shouldRefresh = this._modalRefreshOnClose;
      if (shouldRefresh) this._restorePendingMove();
      this.modalOpen = false;
      this.modalConflicts = [];
      this._retry = null;
      // Refresh so an optimistic DOM change (a resize preview, a sent move)
      // can't sit half-applied; pure validation never touched the DOM.
      if (shouldRefresh) this._refreshBoard();
    },

    // For conflicts: the same mutation resent with override:true. For an
    // unplan: the delete itself, with no override -- a locked session must refuse.
    confirmModal() {
      this.modalOpen = false;
      if (this._retry) {
        this._post(this._retry.action, this._retry.payload);
      }
      this._retry = null;
    },

    // --- now line ---------------------------------------------------------

    _tickNow() {
      if (!this._totalMinutes) return;
      const pct = this._offsetForDate(new Date().toISOString()) / this._totalMinutes * 100;
      document.querySelectorAll(".ps-now-line").forEach((el) => {
        el.style.left = pct + "%";
      });
    },
  };
}

window.productionSchedule = productionSchedule;
