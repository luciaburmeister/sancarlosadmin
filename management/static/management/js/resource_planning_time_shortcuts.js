/*global DateTimeShortcuts, gettext*/
'use strict';

(function () {
    const PLANNING_HOUR_LABELS = {
        7: gettext('7 a.m.'),
        8: gettext('8 a.m.'),
        9: gettext('9 a.m.'),
        10: gettext('10 a.m.'),
        11: gettext('11 a.m.'),
        12: gettext('Noon'),
        13: gettext('1 p.m.'),
        14: gettext('2 p.m.'),
        15: gettext('3 p.m.'),
        16: gettext('4 p.m.'),
        17: gettext('5 p.m.'),
        18: gettext('6 p.m.'),
        19: gettext('7 p.m.'),
    };

    function applyResourcePlanningClockHours() {
        if (typeof DateTimeShortcuts === 'undefined') {
            return;
        }
        DateTimeShortcuts.clockHours.default_ = [];
        for (let hour = 7; hour <= 19; hour++) {
            DateTimeShortcuts.clockHours.default_.push([PLANNING_HOUR_LABELS[hour], hour]);
        }
    }

    window.applyResourcePlanningClockHours = applyResourcePlanningClockHours;
    applyResourcePlanningClockHours();
})();
