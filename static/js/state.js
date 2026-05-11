// Shared application state for the Open-AGC frontend
export const state = {
  ws: null,
  isConnected: false,
  isAgentThinking: false,
  currentSessionId: parseInt(localStorage.getItem('lastSessionId') || '1'),
  sessions: [],
  currentLang: 'zh-CN',
  wasVoiceQuery: false,
  currentTaskId: null,
  progressSteps: {},
  progressStepData: {},
  progressContainer: null,
  currentStatusBubble: null,
  pendingImages: [],
  downloadResumeInfo: null,
  settingsLoaded: false,
  skillsLoaded: false,
  taskFilter: 'all',
  taskSearchQuery: '',
  taskRefreshInterval: null,
  editingAgentName: null,
  aiDesignResult: null,
  aiDesignAbort: null,
};

// DOM element references (lazily accessed via getElementById in each module)
// No need to cache them centrally — modules call document.getElementById() as needed
