// Test in-between string concatenation with variable
const axios = require('axios');
const name = 'johndoe'
axios.post('https://petstore.swagger.io/v2/customers/' + name + '/ping', {});