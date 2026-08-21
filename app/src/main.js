import { mount } from 'svelte';
import './net.svelte.js'; // patches fetch — must run before anything else fetches
import App from './App.svelte';

mount(App, { target: document.getElementById('app') });
