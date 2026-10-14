"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.CatalystDataStreamError = void 0;
const utils_1 = require("@zcatalyst/utils");
class CatalystDataStreamError extends utils_1.PrefixedCatalystError {
    constructor(code, message, value) {
        super('app', code, message, value);
    }
}
exports.CatalystDataStreamError = CatalystDataStreamError;
