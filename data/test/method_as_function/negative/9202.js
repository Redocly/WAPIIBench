// Test object request body when string is expected
const axios = require('axios');
axios.post('https://petstore.swagger.io/markdown/raw', {
  content: "**raw** _Markdown_ document"
});