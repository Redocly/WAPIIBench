// Test variable for string request body
const axios = require('axios');
const content = "**raw** _Markdown_ document"
axios.post('https://petstore.swagger.io/v2/markdown/raw', content);