// Test in-between string concatenation with value
const axios = require('axios');
axios.post('https://petstore.swagger.io/v2/customers/' + 'johndoe' + '/ping', {});