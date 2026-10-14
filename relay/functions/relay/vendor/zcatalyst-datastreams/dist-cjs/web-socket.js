"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || function (mod) {
    if (mod && mod.__esModule) return mod;
    var result = {};
    if (mod != null) for (var k in mod) if (k !== "default" && Object.prototype.hasOwnProperty.call(mod, k)) __createBinding(result, mod, k);
    __setModuleDefault(result, mod);
    return result;
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.DataStreamsWebSocket = void 0;
const utils_1 = require("@zcatalyst/utils");
const enum_js_1 = require("./utils/enum.js");
const event_emitter_js_1 = require("./utils/event-emitter.js");
const validators_js_1 = require("./utils/validators.js");
class DataStreamsWebSocket extends event_emitter_js_1.EventEmitter {
    constructor(config) {
        super();
        this.prdValue = 'CY';
        this.path = '/wsconnect';
        this.sid = '';
        this.uid = '';
        this.finalUrl = '';
        this.conn = null;
        this.isOpen = false;
        this.ackSent = false;
        this.reconnect = false;
        this.prevStreamingId = '';
        this.fallbackConnectionAttempts = 0;
        this.pingInterval = null;
        this.reconnectInterval = null;
        this.pingServerInterval = null;
        this.subscribePayload = {
            type: 'con',
            value: 'subscribe',
            streamingId: ''
        };
        this.ackPayload = {
            type: 'ack',
            streamingId: ''
        };
        this.unsubscribePayload = {
            type: 'con',
            value: 'unsubscribe'
        };
        this.serverPingPayload = {
            type: 'con',
            value: 'ping'
        };
        (0, utils_1.ObjectHasProperties)(config, ['url', 'key', 'zuid'], 'DataStreamsConfig');
        (0, utils_1.isNonEmptyString)(config.url, 'url', true);
        (0, utils_1.isNonEmptyString)(config.key, 'key', true);
        (0, utils_1.isNonEmptyString)(config.zuid, 'zuid', true);
        const validatedUrl = (0, validators_js_1.assertValidHost)(config.url, 'url');
        this.config = {
            enableLogging: config.enableLogging || false,
            ...config,
            url: validatedUrl
        };
        this.url = this.config.url;
        this.keyValue = this.config.key;
        this.zuidValue = this.config.zuid;
        this.finalUrl = `wss://${this.url}${this.path}?prd=${this.prdValue}&zuid=${this.zuidValue}&key=${this.keyValue}`;
        this.createWebSocketConnection();
    }
    async createWebSocketConnection() {
        try {
            const verifiedUrl = (0, validators_js_1.assertValidWebSocketUrl)(this.finalUrl, this.url);
            let WebSocketConstructor;
            if (typeof window !== 'undefined' && window.WebSocket) {
                WebSocketConstructor = window.WebSocket;
            }
            else if (typeof global !== 'undefined') {
                try {
                    const ws = await Promise.resolve().then(() => __importStar(require('ws')));
                    WebSocketConstructor = ws.default;
                }
                catch (importError) {
                    try {
                        const WebSocketModule = require('ws');
                        WebSocketConstructor = WebSocketModule;
                    }
                    catch (requireError) {
                        throw new Error('WebSocket implementation not available. Please ensure you are running in a browser or have the "ws" package installed for Node.js');
                    }
                }
            }
            else {
                throw new Error('WebSocket not available in this environment');
            }
            this.conn = new WebSocketConstructor(verifiedUrl.href);
            this.setupEventHandlers();
        }
        catch (error) {
            const errorMessage = error instanceof Error
                ? error.message
                : 'Unknown error creating WebSocket connection';
            this.log(`Error creating WebSocket connection: ${errorMessage}`);
            const customError = {
                code: 1006,
                message: `Failed to create WebSocket connection: ${errorMessage}`
            };
            this.emit('error', customError);
        }
    }
    setupEventHandlers() {
        if (!this.conn)
            return;
        if (typeof this.conn.on === 'function') {
            this.conn.on('open', (event) => this.handleWebSocketOpenEvent(event));
            this.conn.on('close', (event) => this.handleWebSocketCloseEvent(event));
            this.conn.on('message', (data) => this.handleDMSEvents(data));
            this.conn.on('error', (event) => this.handleWebSocketErrorEvent(event));
        }
        else {
            const browserWs = this.conn;
            browserWs.onopen = (event) => this.handleWebSocketOpenEvent(event);
            browserWs.onclose = (event) => this.handleWebSocketCloseEvent(event);
            browserWs.onmessage = (event) => this.handleDMSEvents(event.data);
            browserWs.onerror = (event) => this.handleWebSocketErrorEvent(event);
        }
    }
    log(message) {
        if (this.config.enableLogging) {
            console.log(`[DataStreams] ${message}`);
        }
    }
    handleWebSocketOpenEvent(event) {
        this.log('WebSocket connection opened');
        if (this.reconnect) {
            this.log('Reconnection event successful!');
            if (this.conn && this.conn.readyState === this.conn.OPEN) {
                this.log(`Subscribe payload: ${JSON.stringify(this.subscribePayload)}`);
                if (this.ackSent) {
                    this.subscribePayload.streamingId = '-2';
                    this.conn.send(JSON.stringify(this.subscribePayload));
                }
            }
            this.startPing();
            this.startPingToServer();
            this.startCloseAndReconnect();
            this.reconnect = false;
        }
        this.isOpen = true;
    }
    handleWebSocketCloseEvent(event) {
        this.emit('close', event);
        this.clearPingInterval();
        this.clearReconnectInterval();
        this.isOpen = false;
    }
    handleWebSocketErrorEvent(event) {
        try {
            this.log(`WebSocket error: ${this.safeStringify(event)}`);
        }
        catch (loggingError) {
            this.log(`WebSocket error (failed to stringify event: ${loggingError instanceof Error ? loggingError.message : String(loggingError)})`);
        }
        this.emit('error', event);
    }
    safeStringify(value) {
        const seen = new WeakSet();
        return JSON.stringify(value, (_key, val) => {
            if (val instanceof Error) {
                return { name: val.name, message: val.message, stack: val.stack };
            }
            if (typeof val === 'object' && val !== null) {
                if (seen.has(val)) {
                    return '[Circular]';
                }
                seen.add(val);
            }
            return val;
        });
    }
    handleDMSEvents(event) {
        const actualData = event;
        if (!actualData || actualData.toString() === '') {
            const customEvent = { message: 'Pong received' };
            this.emit('pong', customEvent);
            return;
        }
        try {
            const jsonData = JSON.parse(actualData.toString());
            const mtype = jsonData[0]?.mtype;
            switch (mtype) {
                case enum_js_1.MessageType.SWITCH_URL: {
                    const msg = jsonData[0].msg;
                    const newHost = msg[msg.primarydc];
                    let validatedHost;
                    try {
                        validatedHost = (0, validators_js_1.assertValidHost)(newHost, 'url');
                    }
                    catch (validationError) {
                        const errorEvent = {
                            code: 1007,
                            message: 'Received an invalid host in the switch-url message.'
                        };
                        this.emit('error', errorEvent);
                        break;
                    }
                    this.url = validatedHost;
                    if (this.conn && this.conn.readyState === this.conn.OPEN) {
                        this.conn.close(1000, 'Closing this connection and opening new connection!');
                        this.makeNewConnection();
                    }
                    break;
                }
                case enum_js_1.MessageType.MISSING_KEY: {
                    const errorEvent = {
                        code: 1014,
                        message: 'Mandatory param key is missing'
                    };
                    this.emit('error', errorEvent);
                    break;
                }
                case enum_js_1.MessageType.AUTH_FAILED: {
                    this.log('-5 triggered');
                    const errorEvent = {
                        code: 3000,
                        message: 'Authentication failed, please generate valid credentials and try again after some time!'
                    };
                    this.emit('error', errorEvent);
                    break;
                }
                case enum_js_1.MessageType.SESSION_EXPIRED: {
                    const isKeyMismatch = String(jsonData[0]?.reason ?? '').includes('key_mismatch');
                    const invalidRequestFailure = String(jsonData[0]?.reason ?? '').includes('Invalid Request');
                    const isConnectionAlive = isKeyMismatch || invalidRequestFailure;
                    if (isConnectionAlive && this.fallbackConnectionAttempts === 0) {
                        this.fallbackConnectionAttempts += 1;
                        this.log('SESSION_EXPIRED with key_mismatch reason; previous connection still alive, retrying connect() once');
                        this.createWebSocketConnection();
                    }
                    else {
                        const errorEvent = {
                            code: 1000,
                            message: 'Current session will be closed. New connection to be established.'
                        };
                        this.emit('error', errorEvent);
                        this.log('WebSocket connection got expired. Please generate new credentials to connect again');
                    }
                    break;
                }
                case enum_js_1.MessageType.AUTH_SUCCESS: {
                    const msg = jsonData[0].msg;
                    this.sid = msg.sid;
                    this.uid = msg.uid;
                    const openEvent = {
                        code: 200,
                        message: 'Streams connection established, please subscribe with appropriate subscribe type to start streaming.'
                    };
                    this.emit('open', openEvent);
                    this.startPing();
                    this.startPingToServer();
                    this.startCloseAndReconnect();
                    break;
                }
                case enum_js_1.MessageType.BLOCKED: {
                    const customEvent = {
                        code: 1013,
                        message: 'Data streams connection is blocked! Please contact support!'
                    };
                    this.emit('error', customEvent);
                    break;
                }
                case enum_js_1.MessageType.DATA_EVENT: {
                    this.handleDataEvents(jsonData);
                    break;
                }
                case enum_js_1.MessageType.SERVER_DOWN: {
                    const customEvent = {
                        code: 1011,
                        message: 'Internal server error. Please retry after a minute'
                    };
                    this.log('-> 670 server is down');
                    this.startPingToServer();
                    this.emit('error', customEvent);
                    break;
                }
                default:
                    this.log(`Unknown message type: ${mtype}`);
                    break;
            }
        }
        catch (error) {
            this.log(`Error parsing WebSocket message: ${error instanceof Error ? error.message : 'Unknown error'}`);
        }
    }
    isWebSocketOpen() {
        return this.conn !== null && this.conn.readyState === this.conn.OPEN;
    }
    handleDataEvents(jsonData) {
        for (let i = 0; i < jsonData.length; i++) {
            const msg = jsonData[i].msg;
            const data = msg.data;
            if (this.prevStreamingId === data.streamingId && this.ackSent) {
                this.log(`prev :: ${this.prevStreamingId}, current :: ${data.streamingId}`);
                this.ackPayload.streamingId = this.prevStreamingId;
                if (this.isWebSocketOpen()) {
                    this.conn.send(JSON.stringify(this.ackPayload));
                }
            }
            else {
                const operation = msg.opr;
                if (operation === 'event') {
                    this.ackSent = false;
                    this.prevStreamingId = data.streamingId;
                    this.ackPayload.streamingId = data.streamingId;
                    const customEvent = {
                        data: data.data,
                        streamingId: data.streamingId
                    };
                    this.emit('message', customEvent);
                }
                else if (operation === 'api') {
                    this.ackSent = false;
                    this.ackPayload.streamingId = data.streamingId;
                    this.prevStreamingId = data.streamingId;
                    const customEvent = {
                        streamingId: data.streamingId,
                        url: data.url,
                        method: data.method
                    };
                    this.emit('message', customEvent);
                }
                else if (operation === 'connect') {
                    if (this.ackSent && this.isWebSocketOpen()) {
                        const ackPayload = { ...this.ackPayload };
                        ackPayload.streamingId = '-2';
                        this.log('Ack payload for pong');
                        this.conn.send(JSON.stringify(ackPayload));
                    }
                }
                else if (operation === 'error') {
                    const errorEvent = {
                        error: data.value
                    };
                    if (data.code === 'e3' || data.code === 'e5') {
                        errorEvent.error = 'Subscription failed. Invalid subscription type.';
                    }
                    this.emit('error', errorEvent);
                }
            }
        }
    }
    makeNewConnection() {
        if (this.sid && this.uid) {
            this.finalUrl = `wss://${this.url}${this.path}?key=${this.keyValue}&i=${this.sid}&c=${this.uid}`;
            this.log(`Reconnecting to ${this.url}${this.path}`);
            this.createWebSocketConnection();
        }
    }
    clearPingInterval() {
        if (this.pingInterval) {
            clearInterval(this.pingInterval);
            this.pingInterval = null;
        }
    }
    clearReconnectInterval() {
        if (this.reconnectInterval) {
            clearTimeout(this.reconnectInterval);
            this.reconnectInterval = null;
        }
    }
    clearPingServerInterval() {
        if (this.pingServerInterval) {
            clearInterval(this.pingServerInterval);
            this.pingServerInterval = null;
        }
    }
    startPing() {
        this.clearPingInterval();
        this.pingInterval = setInterval(() => {
            if (this.conn && this.conn.readyState === this.conn.OPEN) {
                this.log('-');
                this.conn.send('-');
            }
            else {
                this.log('Clearing ping interval');
                this.clearPingInterval();
            }
        }, 15000);
    }
    startPingToServer() {
        this.clearPingServerInterval();
        this.pingServerInterval = setInterval(() => {
            if (this.conn && this.conn.readyState === this.conn.OPEN) {
                this.log('Ping server to reconnect');
                this.conn.send(JSON.stringify(this.serverPingPayload));
            }
            else {
                this.log('Clearing ping server interval');
                this.clearPingServerInterval();
            }
        }, 60000);
    }
    startCloseAndReconnect() {
        this.clearReconnectInterval();
        this.reconnectInterval = setTimeout(() => {
            if (this.conn && this.conn.readyState === this.conn.OPEN) {
                this.clearPingInterval();
                this.clearReconnectInterval();
                this.clearPingServerInterval();
                this.log('Closing current connection and making new connection using sid and uid');
                this.conn.close(1000, 'Closing this connection and opening new connection!');
                setTimeout(() => {
                    this.reconnect = true;
                    this.makeNewConnection();
                }, 50);
            }
        }, 27 * 60 * 1000);
    }
    subscribe(subscribeType = '0') {
        if (subscribeType === undefined) {
            throw new Error('Subscribe type is required');
        }
        this.subscribePayload.streamingId = subscribeType;
        if (this.conn && this.conn.readyState === this.conn.OPEN) {
            this.log(`Subscribing with payload: ${JSON.stringify(this.subscribePayload)}`);
            this.conn.send(JSON.stringify(this.subscribePayload));
        }
        else {
            throw new Error('WebSocket connection is not open');
        }
    }
    unsubscribe() {
        if (this.conn && this.conn.readyState === this.conn.OPEN) {
            this.conn.send(JSON.stringify(this.unsubscribePayload));
            this.log('Unsubscribed from channel');
        }
        else {
            throw new Error('WebSocket connection is not open');
        }
    }
    sendAck() {
        if (this.conn &&
            this.conn.readyState === this.conn.OPEN &&
            this.ackPayload.streamingId !== '') {
            this.conn.send(JSON.stringify(this.ackPayload));
            this.ackSent = true;
            this.log(`Sent ack for streaming ID: ${this.ackPayload.streamingId}`);
        }
        else {
            this.log('Cannot send ack: connection not open or no streaming ID');
        }
    }
    close() {
        if (this.conn && this.conn.readyState === this.conn.OPEN) {
            this.conn.close(1000, 'Closing connection intentionally');
            this.log('WebSocket connection closed manually');
        }
        this.clearPingInterval();
        this.clearReconnectInterval();
        this.clearPingServerInterval();
    }
    isConnected() {
        return this.conn !== null && this.conn.readyState === this.conn.OPEN && this.isOpen;
    }
    getConnectionState() {
        if (!this.conn)
            return 'disconnected';
        switch (this.conn.readyState) {
            case this.conn.CONNECTING:
                return 'connecting';
            case this.conn.OPEN:
                return 'connected';
            case this.conn.CLOSING:
                return 'closing';
            case this.conn.CLOSED:
                return 'closed';
            default:
                return 'unknown';
        }
    }
    getSessionInfo() {
        return {
            sid: this.sid,
            uid: this.uid
        };
    }
}
exports.DataStreamsWebSocket = DataStreamsWebSocket;
