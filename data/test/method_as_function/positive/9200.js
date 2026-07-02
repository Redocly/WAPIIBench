// Test string request body
const axios = require('axios');
axios.post('https://petstore.swagger.io/v2/markdown/raw', "**raw** _Markdown_ document");