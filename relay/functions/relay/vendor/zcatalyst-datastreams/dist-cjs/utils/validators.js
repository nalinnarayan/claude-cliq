"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.assertValidWebSocketUrl = exports.assertValidHost = void 0;
const errors_js_1 = require("./errors.js");
const ALLOWED_DATASTREAM_HOSTS = new Set([
    'us4-dms.zoho.com',
    'us3-dms.zoho.com',
    'predms.zoho.com',
    'eu1-dms.zoho.eu',
    'eu2-dms.zoho.eu',
    'in2-dms.zoho.in',
    'in1-dms.zoho.in',
    'au1-dms.zoho.com.au',
    'au2-dms.zoho.com.au',
    'cn2-dms.zoho.com.cn',
    'cn3-dms.zoho.com.cn',
    'jp1-dms.zoho.jp',
    'jp2-dms.zoho.jp',
    'ca1-dms.zohocloud.ca',
    'ca2-dms.zohocloud.ca',
    'uk1-dms.zoho.uk',
    'uk2-dms.zoho.uk',
    'local-dms.localzoho.com',
    'ct2-dms.localzoho.com',
    'predms.localzoho.com',
    'ct2-predms.localzoho.com',
    'sa1-dms.zoho.sa',
    'sa2-dms.zoho.sa',
    'uae1-dms.zoho.ae',
    'uae2-dms.zoho.ae'
].map((host) => host.toLowerCase()));
function assertValidHost(host, name) {
    if (typeof host !== 'string' || host === '') {
        throw new errors_js_1.CatalystDataStreamError('INVALID_ARGUMENT', `Value provided for ${name} must be a non-empty String.`, host);
    }
    const normalizedHost = host.toLowerCase();
    if (!ALLOWED_DATASTREAM_HOSTS.has(normalizedHost)) {
        throw new errors_js_1.CatalystDataStreamError('INVALID_ARGUMENT', `Value provided for ${name} is not a recognized DataStreams host.`, host);
    }
    return normalizedHost;
}
exports.assertValidHost = assertValidHost;
function assertValidWebSocketUrl(wsUrl, expectedHost) {
    let parsed;
    try {
        parsed = new URL(wsUrl);
    }
    catch {
        throw new errors_js_1.CatalystDataStreamError('INVALID_ARGUMENT', 'Unable to construct a valid WebSocket URL for the given host.', wsUrl);
    }
    if (parsed.protocol !== 'wss:' ||
        parsed.username !== '' ||
        parsed.password !== '' ||
        parsed.hostname.toLowerCase() !== expectedHost.toLowerCase()) {
        throw new errors_js_1.CatalystDataStreamError('INVALID_ARGUMENT', 'The constructed WebSocket URL does not match the expected DataStreams host.', wsUrl);
    }
    return parsed;
}
exports.assertValidWebSocketUrl = assertValidWebSocketUrl;
