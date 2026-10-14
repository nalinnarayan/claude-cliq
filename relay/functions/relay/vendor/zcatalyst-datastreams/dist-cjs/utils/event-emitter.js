"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.EventEmitter = void 0;
class EventEmitter {
    constructor() {
        this.events = new Map();
    }
    on(event, listener) {
        if (!this.events.has(event)) {
            this.events.set(event, new Set());
        }
        this.events.get(event).add(listener);
        return this;
    }
    once(event, listener) {
        const onceWrapper = (...args) => {
            this.off(event, onceWrapper);
            listener(...args);
        };
        return this.on(event, onceWrapper);
    }
    off(event, listener) {
        const listeners = this.events.get(event);
        if (listeners) {
            listeners.delete(listener);
            if (listeners.size === 0) {
                this.events.delete(event);
            }
        }
        return this;
    }
    removeAllListeners(event) {
        if (event) {
            this.events.delete(event);
        }
        else {
            this.events.clear();
        }
        return this;
    }
    emit(event, ...args) {
        const listeners = this.events.get(event);
        if (!listeners || listeners.size === 0) {
            return false;
        }
        const listenersArray = Array.from(listeners);
        for (const listener of listenersArray) {
            try {
                listener(...args);
            }
            catch (error) {
                if (event !== 'error') {
                    this.emit('error', error);
                }
                else {
                    console.error('EventEmitter error:', error);
                }
            }
        }
        return true;
    }
    listenerCount(event) {
        const listeners = this.events.get(event);
        return listeners ? listeners.size : 0;
    }
    listeners(event) {
        const listeners = this.events.get(event);
        return listeners ? Array.from(listeners) : [];
    }
}
exports.EventEmitter = EventEmitter;
