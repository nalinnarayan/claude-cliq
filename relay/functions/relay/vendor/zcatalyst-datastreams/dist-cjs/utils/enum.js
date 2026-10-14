"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.MessageType = void 0;
var MessageType;
(function (MessageType) {
    MessageType["SWITCH_URL"] = "-1";
    MessageType["MISSING_KEY"] = "-2";
    MessageType["AUTH_FAILED"] = "-5";
    MessageType["SESSION_EXPIRED"] = "-11";
    MessageType["AUTH_SUCCESS"] = "0";
    MessageType["BLOCKED"] = "660";
    MessageType["DATA_EVENT"] = "650";
    MessageType["SERVER_DOWN"] = "670";
})(MessageType || (exports.MessageType = MessageType = {}));
