"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.DataStreamsAdmin = exports.DataStreams = void 0;
const transport_1 = require("@zcatalyst/transport");
const utils_1 = require("@zcatalyst/utils");
const package_json_1 = __importDefault(require("../package.json"));
const { version } = package_json_1.default;
const errors_js_1 = require("./utils/errors.js");
const { COMPONENT, REQ_METHOD, CREDENTIAL_USER, X_ZOHO_CATALYST_RESOURCE_ID } = utils_1.CONSTANTS;
class DataStreams {
    constructor(app) {
        this.requester = new transport_1.Handler(app, this);
    }
    getComponentName() {
        return COMPONENT.data_streams;
    }
    getComponentVersion() {
        return version;
    }
    async getTokenPair(channelId, { userId, connectionName }) {
        await (0, utils_1.wrapValidatorsWithPromise)(() => {
            (0, utils_1.isValidInputString)(channelId, 'channelId', true);
        }, errors_js_1.CatalystDataStreamError);
        if (!(0, utils_1.isValidInputString)(userId, 'userId', false) &&
            !(0, utils_1.isValidInputString)(connectionName, 'connectionName', false)) {
            throw new errors_js_1.CatalystDataStreamError('INVALID_PARAM', 'Either userId or connectionName must be provided and valid.');
        }
        let payload = {};
        if (userId) {
            payload = { app_user_id: userId };
        }
        else if (connectionName) {
            payload = { connection_name: connectionName };
        }
        const request = {
            method: REQ_METHOD.post,
            path: `/datastreams/channel/${channelId}/tokenpair`,
            data: payload,
            headers: typeof window === 'undefined' ? this._addHeader({}) : {},
            type: "json",
            expecting: "json",
            user: CREDENTIAL_USER.user,
            service: "baas"
        };
        const resp = await this.requester.send(request);
        return resp.data.data;
    }
    _addHeader(headers) {
        const resourceId = process.env.X_ZOHO_CATALYST_RESOURCE_ID;
        if ((0, utils_1.isNonEmptyString)(resourceId)) {
            headers[X_ZOHO_CATALYST_RESOURCE_ID] = resourceId;
        }
        return headers;
    }
}
exports.DataStreams = DataStreams;
class DataStreamsAdmin extends DataStreams {
    constructor(app) {
        super(app);
    }
    async getAllChannels() {
        const request = {
            method: REQ_METHOD.get,
            path: '/datastreams/channel',
            type: "json",
            expecting: "json",
            user: CREDENTIAL_USER.admin,
            service: "baas"
        };
        const resp = await this.requester.send(request);
        return resp.data.data;
    }
    async getChannelDetails(channelId) {
        await (0, utils_1.wrapValidatorsWithPromise)(() => {
            (0, utils_1.isValidInputString)(channelId, 'channelId', true);
        }, errors_js_1.CatalystDataStreamError);
        const request = {
            method: REQ_METHOD.get,
            path: `/datastreams/channel/${channelId}`,
            type: "json",
            expecting: "json",
            user: CREDENTIAL_USER.admin,
            service: "baas"
        };
        const resp = await this.requester.send(request);
        return resp.data.data;
    }
    async getLiveCount(channelId) {
        await (0, utils_1.wrapValidatorsWithPromise)(() => {
            (0, utils_1.isValidInputString)(channelId, 'channelId', true);
        }, errors_js_1.CatalystDataStreamError);
        const request = {
            method: REQ_METHOD.get,
            path: `/datastreams/channel/${channelId}/liveclient`,
            type: "json",
            expecting: "json",
            user: CREDENTIAL_USER.admin,
            service: "baas"
        };
        const resp = await this.requester.send(request);
        return resp.data;
    }
    async publishData(channelId, data) {
        await (0, utils_1.wrapValidatorsWithPromise)(() => {
            (0, utils_1.isValidInputString)(channelId, 'channelId', true);
            (0, utils_1.isNonEmptyString)(data, 'data', true);
        }, errors_js_1.CatalystDataStreamError);
        const payload = data;
        const request = {
            method: REQ_METHOD.post,
            path: `/datastreams/channel/${channelId}/stream`,
            data: { data: payload },
            type: "json",
            expecting: "json",
            user: CREDENTIAL_USER.admin,
            service: "baas"
        };
        const resp = await this.requester.send(request);
        return resp.data.data;
    }
}
exports.DataStreamsAdmin = DataStreamsAdmin;
