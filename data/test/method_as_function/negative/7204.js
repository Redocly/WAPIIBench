// Test in-between string concatenation with wrong data type
const axios = require('axios');
axios.post('https://petstore.swagger.io/v2/customers/' + 1234 + '/ping', {});