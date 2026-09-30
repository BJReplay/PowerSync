export const LEGACY_SCENE_IMAGES = new Set(["image.png", "image2.png"]);

export const SCENE_IMAGE_MAP = Object.freeze({
  "day_clear_charging": "scene_day_clear_charging.png",
  "day_clear_idle": "scene_day_clear_idle.png",
  "day_cloudy_charging": "scene_day_cloudy_charging.png",
  "day_cloudy_idle": "scene_day_cloudy_idle.png",
  "day_rain_charging": "scene_day_rain_charging.png",
  "day_rain_idle": "scene_day_rain_idle.png",
  "day_snow_charging": "scene_day_snow_charging.png",
  "day_snow_idle": "scene_day_snow_idle.png",
  "day_storm_charging": "scene_day_storm_charging.png",
  "day_storm_idle": "scene_day_storm_idle.png",
  "night_clear_charging": "scene_night_clear_charging.png",
  "night_clear_idle": "scene_night_clear_idle.png",
  "night_cloudy_charging": "scene_night_cloudy_charging.png",
  "night_cloudy_idle": "scene_night_cloudy_idle.png",
  "night_rain_charging": "scene_night_rain_charging.png",
  "night_rain_idle": "scene_night_rain_idle.png",
  "night_snow_charging": "scene_night_snow_charging.png",
  "night_snow_idle": "scene_night_snow_idle.png",
  "night_storm_charging": "scene_night_storm_charging.png",
  "night_storm_idle": "scene_night_storm_idle.png",
});

export const DUAL_CHARGING_SCENE_IMAGE_MAP = Object.freeze({
  "day_clear_dual_charging": "scene_day_clear_dual_charging.png",
  "day_cloudy_dual_charging": "scene_day_cloudy_dual_charging.png",
  "day_rain_dual_charging": "scene_day_rain_dual_charging.png",
  "day_snow_dual_charging": "scene_day_snow_dual_charging.png",
  "day_storm_dual_charging": "scene_day_storm_dual_charging.png",
  "night_clear_dual_charging": "scene_night_clear_dual_charging.png",
  "night_cloudy_dual_charging": "scene_night_cloudy_dual_charging.png",
  "night_rain_dual_charging": "scene_night_rain_dual_charging.png",
  "night_snow_dual_charging": "scene_night_snow_dual_charging.png",
  "night_storm_dual_charging": "scene_night_storm_dual_charging.png",
});

export const SCENE_FLOW_PATH_MAP = Object.freeze({
  "scene_day_clear_charging.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 374 L 434 402",
    "line-solar-battery": "M 350 292 L 352 338 L 312 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 312 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "M 164 322 Q 160 368 182 344",
    "line-wallbox-ev2": "",
  },
  "scene_day_clear_dual_charging.png": {
    "line-solar-load": "M 394 287 L 401 302 401 337",
    "line-solar-grid": "M 401 341 L 400 378 476 402",
    "line-solar-battery": "M 400 337 L 389 341 354 348",
    "line-grid-load": "M 490 407 Q 441 391 399 376 400 358 400 337",
    "line-grid-battery": "M 490 407 Q 441 391 399 376 400 358 400 337 L 354 348",
    "line-battery-load": "M 355 347 Q 383 342 398 338",
    "line-junction-home-load": "M 401 338 Q 428 332 456 325",
    "line-wallbox-ev": "M 203 323 Q 200 381 220 340",
    "line-wallbox-ev2": "M 174 310 Q 161 384 126 315",
  },
  "scene_day_clear_idle.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 378 L 436 404",
    "line-solar-battery": "M 350 292 L 352 340 L 310 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 310 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "",
    "line-wallbox-ev2": "",
  },
  "scene_day_cloudy_charging.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 374 L 434 402",
    "line-solar-battery": "M 350 292 L 352 338 L 312 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 312 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "M 164 322 Q 160 368 182 344",
    "line-wallbox-ev2": "",
  },
  "scene_day_cloudy_dual_charging.png": {
    "line-solar-load": "M 394 287 L 401 302 401 337",
    "line-solar-grid": "M 401 341 L 400 378 476 402",
    "line-solar-battery": "M 400 337 L 389 341 354 348",
    "line-grid-load": "M 490 407 Q 441 391 399 376 400 358 400 337",
    "line-grid-battery": "M 490 407 Q 441 391 399 376 400 358 400 337 L 354 348",
    "line-battery-load": "M 355 347 Q 383 342 398 338",
    "line-junction-home-load": "M 401 338 Q 428 332 456 325",
    "line-wallbox-ev": "M 203 323 Q 200 381 220 340",
    "line-wallbox-ev2": "M 174 310 Q 161 384 126 315",
  },
  "scene_day_cloudy_idle.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 378 L 436 404",
    "line-solar-battery": "M 350 292 L 352 340 L 310 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 310 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "",
    "line-wallbox-ev2": "",
  },
  "scene_day_rain_charging.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 374 L 434 402",
    "line-solar-battery": "M 350 292 L 352 338 L 312 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 312 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "M 164 322 Q 160 368 182 344",
    "line-wallbox-ev2": "",
  },
  "scene_day_rain_dual_charging.png": {
    "line-solar-load": "M 394 287 L 401 302 401 337",
    "line-solar-grid": "M 401 341 L 400 378 476 402",
    "line-solar-battery": "M 400 337 L 389 341 354 348",
    "line-grid-load": "M 490 407 Q 441 391 399 376 400 358 400 337",
    "line-grid-battery": "M 490 407 Q 441 391 399 376 400 358 400 337 L 354 348",
    "line-battery-load": "M 355 347 Q 383 342 398 338",
    "line-junction-home-load": "M 401 338 Q 428 332 456 325",
    "line-wallbox-ev": "M 203 323 Q 200 381 220 340",
    "line-wallbox-ev2": "M 174 310 Q 161 384 126 315",
  },
  "scene_day_rain_idle.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 378 L 436 404",
    "line-solar-battery": "M 350 292 L 352 340 L 310 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 310 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "",
    "line-wallbox-ev2": "",
  },
  "scene_day_snow_charging.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 374 L 434 402",
    "line-solar-battery": "M 350 292 L 352 338 L 312 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 312 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "M 164 322 Q 160 368 182 344",
    "line-wallbox-ev2": "",
  },
  "scene_day_snow_dual_charging.png": {
    "line-solar-load": "M 394 287 L 401 302 401 337",
    "line-solar-grid": "M 401 341 L 400 378 476 402",
    "line-solar-battery": "M 400 337 L 389 341 354 348",
    "line-grid-load": "M 490 407 Q 441 391 399 376 400 358 400 337",
    "line-grid-battery": "M 490 407 Q 441 391 399 376 400 358 400 337 L 354 348",
    "line-battery-load": "M 355 347 Q 383 342 398 338",
    "line-junction-home-load": "M 401 338 Q 428 332 456 325",
    "line-wallbox-ev": "M 203 323 Q 200 381 220 340",
    "line-wallbox-ev2": "M 174 310 Q 161 384 126 315",
  },
  "scene_day_snow_idle.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 378 L 436 404",
    "line-solar-battery": "M 350 292 L 352 340 L 310 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 310 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "",
    "line-wallbox-ev2": "",
  },
  "scene_day_storm_charging.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 374 L 434 402",
    "line-solar-battery": "M 350 292 L 352 338 L 312 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 312 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "M 164 322 Q 160 368 182 344",
    "line-wallbox-ev2": "",
  },
  "scene_day_storm_dual_charging.png": {
    "line-solar-load": "M 394 287 L 401 302 401 337",
    "line-solar-grid": "M 401 341 L 400 378 476 402",
    "line-solar-battery": "M 400 337 L 389 341 354 348",
    "line-grid-load": "M 490 407 Q 441 391 399 376 400 358 400 337",
    "line-grid-battery": "M 490 407 Q 441 391 399 376 400 358 400 337 L 354 348",
    "line-battery-load": "M 355 347 Q 383 342 398 338",
    "line-junction-home-load": "M 401 338 Q 428 332 456 325",
    "line-wallbox-ev": "M 203 323 Q 200 381 220 340",
    "line-wallbox-ev2": "M 174 310 Q 161 384 126 315",
  },
  "scene_day_storm_idle.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 378 L 436 404",
    "line-solar-battery": "M 350 292 L 352 340 L 310 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 310 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "",
    "line-wallbox-ev2": "",
  },
  "scene_night_clear_charging.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 374 L 434 402",
    "line-solar-battery": "M 350 292 L 352 338 L 312 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 312 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "M 164 322 Q 160 368 182 344",
    "line-wallbox-ev2": "",
  },
  "scene_night_clear_dual_charging.png": {
    "line-solar-load": "M 394 287 L 401 302 401 337",
    "line-solar-grid": "M 401 341 L 400 378 476 402",
    "line-solar-battery": "M 400 337 L 389 341 354 348",
    "line-grid-load": "M 490 407 Q 441 391 399 376 400 358 400 337",
    "line-grid-battery": "M 490 407 Q 441 391 399 376 400 358 400 337 L 354 348",
    "line-battery-load": "M 355 347 Q 383 342 398 338",
    "line-junction-home-load": "M 401 338 Q 428 332 456 325",
    "line-wallbox-ev": "M 203 323 Q 200 381 220 340",
    "line-wallbox-ev2": "M 174 310 Q 161 384 126 315",
  },
  "scene_night_clear_idle.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 378 L 436 404",
    "line-solar-battery": "M 350 292 L 352 340 L 310 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 310 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "",
    "line-wallbox-ev2": "",
  },
  "scene_night_cloudy_charging.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 374 L 434 402",
    "line-solar-battery": "M 350 292 L 352 338 L 312 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 312 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "M 164 322 Q 160 368 182 344",
    "line-wallbox-ev2": "",
  },
  "scene_night_cloudy_dual_charging.png": {
    "line-solar-load": "M 394 287 L 401 302 401 337",
    "line-solar-grid": "M 401 341 L 400 378 476 402",
    "line-solar-battery": "M 400 337 L 389 341 354 348",
    "line-grid-load": "M 490 407 Q 441 391 399 376 400 358 400 337",
    "line-grid-battery": "M 490 407 Q 441 391 399 376 400 358 400 337 L 354 348",
    "line-battery-load": "M 355 347 Q 383 342 398 338",
    "line-junction-home-load": "M 401 338 Q 428 332 456 325",
    "line-wallbox-ev": "M 203 323 Q 200 381 220 340",
    "line-wallbox-ev2": "M 174 310 Q 161 384 126 315",
  },
  "scene_night_cloudy_idle.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 378 L 436 404",
    "line-solar-battery": "M 350 292 L 352 340 L 310 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 310 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "",
    "line-wallbox-ev2": "",
  },
  "scene_night_rain_charging.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 374 L 434 402",
    "line-solar-battery": "M 350 292 L 352 338 L 312 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 312 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "M 164 322 Q 160 368 182 344",
    "line-wallbox-ev2": "",
  },
  "scene_night_rain_dual_charging.png": {
    "line-solar-load": "M 394 287 L 401 302 401 337",
    "line-solar-grid": "M 401 341 L 400 378 476 402",
    "line-solar-battery": "M 400 337 L 389 341 354 348",
    "line-grid-load": "M 490 407 Q 441 391 399 376 400 358 400 337",
    "line-grid-battery": "M 490 407 Q 441 391 399 376 400 358 400 337 L 354 348",
    "line-battery-load": "M 355 347 Q 383 342 398 338",
    "line-junction-home-load": "M 401 338 Q 428 332 456 325",
    "line-wallbox-ev": "M 203 323 Q 200 381 220 340",
    "line-wallbox-ev2": "M 174 310 Q 161 384 126 315",
  },
  "scene_night_rain_idle.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 378 L 436 404",
    "line-solar-battery": "M 350 292 L 352 340 L 310 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 310 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "",
    "line-wallbox-ev2": "",
  },
  "scene_night_snow_charging.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 374 L 434 402",
    "line-solar-battery": "M 350 292 L 352 338 L 312 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 312 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "M 164 322 Q 160 368 182 344",
    "line-wallbox-ev2": "",
  },
  "scene_night_snow_dual_charging.png": {
    "line-solar-load": "M 394 287 L 401 302 401 337",
    "line-solar-grid": "M 401 341 L 400 378 476 402",
    "line-solar-battery": "M 400 337 L 389 341 354 348",
    "line-grid-load": "M 490 407 Q 441 391 399 376 400 358 400 337",
    "line-grid-battery": "M 490 407 Q 441 391 399 376 400 358 400 337 L 354 348",
    "line-battery-load": "M 355 347 Q 383 342 398 338",
    "line-junction-home-load": "M 401 338 Q 428 332 456 325",
    "line-wallbox-ev": "M 203 323 Q 200 381 220 340",
    "line-wallbox-ev2": "M 174 310 Q 161 384 126 315",
  },
  "scene_night_snow_idle.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 378 L 436 404",
    "line-solar-battery": "M 350 292 L 352 340 L 310 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 310 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "",
    "line-wallbox-ev2": "",
  },
  "scene_night_storm_charging.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 374 L 434 402",
    "line-solar-battery": "M 350 292 L 352 338 L 312 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 312 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "M 164 322 Q 160 368 182 344",
    "line-wallbox-ev2": "",
  },
  "scene_night_storm_dual_charging.png": {
    "line-solar-load": "M 394 287 L 401 302 401 337",
    "line-solar-grid": "M 401 341 L 400 378 476 402",
    "line-solar-battery": "M 400 337 L 389 341 354 348",
    "line-grid-load": "M 490 407 Q 441 391 399 376 400 358 400 337",
    "line-grid-battery": "M 490 407 Q 441 391 399 376 400 358 400 337 L 354 348",
    "line-battery-load": "M 355 347 Q 383 342 398 338",
    "line-junction-home-load": "M 401 338 Q 428 332 456 325",
    "line-wallbox-ev": "M 203 323 Q 200 381 220 340",
    "line-wallbox-ev2": "M 174 310 Q 161 384 126 315",
  },
  "scene_night_storm_idle.png": {
    "line-solar-load": "M 351 292 L 352 338 L 352 338",
    "line-solar-grid": "M 350 292 L 352 378 L 436 404",
    "line-solar-battery": "M 350 292 L 352 340 L 310 348",
    "line-grid-load": "M 434 402 Q 434 402 351 375 Q 352 340 351 341",
    "line-grid-battery": "M 434 402 Q 434 402 351 375 Q 352 340 352 338 L 310 348",
    "line-battery-load": "M 310 348 Q 353 339 352 338",
    "line-junction-home-load": "M 354 338 Q 386 330 408 324",
    "line-wallbox-ev": "",
    "line-wallbox-ev2": "",
  },
});

export const SCENE_FLOW_COMPONENT_MAP = Object.freeze({
  "scene_day_clear_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 423.85851518026567,
      "y": 440.27593007966584,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 280.48283325426945,
      "y": 445.5638487121974,
    },
    "battery-direction-arrow": {
      "x": 339.1744248102467,
      "y": 455.8683966277361,
    },
    "ev-label": {
      "x": 170.05529530360533,
      "y": 258.72776316807176,
    },
    "ev-power": {
      "x": 169.44156190702088,
      "y": 280.4509242787532,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.9371145635673,
      "y1": 389.16234820945164,
      "x2": 431.9749169829222,
      "y2": 339.51862092969293,
    },
    "load-guide": {
      "x1": 405.91348434535104,
      "y1": 250.31334596643208,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_day_clear_dual_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 327.06653225806457,
      "y": 80.40171320287725,
    },
    "grid-label": {
      "x": 492.1541449240987,
      "y": 398.98213318895506,
    },
    "grid-power": {
      "x": 491.424780597723,
      "y": 418.8529662000155,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 336.328125,
      "y": 407.46326088637943,
    },
    "battery-label": {
      "x": 336.71726755218214,
      "y": 423.4537667259649,
    },
    "battery-power": {
      "x": 337.76684060721067,
      "y": 438.21457575991957,
    },
    "battery-direction-arrow": {
      "x": 392.7404530360531,
      "y": 449.7377020651249,
    },
    "ev-label": {
      "x": 225.27128795066412,
      "y": 269.4726003557893,
    },
    "ev-power": {
      "x": 224.41072699240988,
      "y": 291.1646299017712,
    },
    "ev-pct": {
      "x": 225.0,
      "y": 327.0,
    },
    "ev2-label": {
      "x": 102.2266366223909,
      "y": 226.49769897130483,
    },
    "ev2-power": {
      "x": 100.47883064516128,
      "y": 248.060754892103,
    },
    "ev2-pct": {
      "x": 133.81389350094878,
      "y": 296.33469332508315,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 479.50367647058823,
      "y1": 393.124951659061,
      "x2": 482.0920303605313,
      "y2": 341.8757251140846,
    },
    "load-guide": {
      "x1": 406.0,
      "y1": 242.0,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 316.2016722011385,
      "y1": 429.535540258334,
      "x2": 317.88276802656543,
      "y2": 374.02573671591,
    },
  },
  "scene_day_clear_idle.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 425.17715251423147,
      "y": 442.7264289581561,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 279.84686314041744,
      "y": 445.31924356098693,
    },
    "battery-direction-arrow": {
      "x": 338.30496916508537,
      "y": 456.46212004021965,
    },
    "ev-label": {
      "x": 162.0,
      "y": 177.0,
    },
    "ev-power": {
      "x": 178.0,
      "y": 205.0,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.94823292220116,
      "y1": 390.9034921494315,
      "x2": 432.0505218216319,
      "y2": 342.5784090030165,
    },
    "load-guide": {
      "x1": 405.88902395635677,
      "y1": 248.8145834944698,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_day_cloudy_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 423.85851518026567,
      "y": 440.27593007966584,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 280.48283325426945,
      "y": 445.5638487121974,
    },
    "battery-direction-arrow": {
      "x": 339.1744248102467,
      "y": 455.8683966277361,
    },
    "ev-label": {
      "x": 170.05529530360533,
      "y": 258.72776316807176,
    },
    "ev-power": {
      "x": 169.44156190702088,
      "y": 280.4509242787532,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.9371145635673,
      "y1": 389.16234820945164,
      "x2": 431.9749169829222,
      "y2": 339.51862092969293,
    },
    "load-guide": {
      "x1": 405.91348434535104,
      "y1": 250.31334596643208,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_day_cloudy_dual_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 327.06653225806457,
      "y": 80.40171320287725,
    },
    "grid-label": {
      "x": 492.1541449240987,
      "y": 398.98213318895506,
    },
    "grid-power": {
      "x": 491.424780597723,
      "y": 418.8529662000155,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 336.328125,
      "y": 407.46326088637943,
    },
    "battery-label": {
      "x": 336.71726755218214,
      "y": 423.4537667259649,
    },
    "battery-power": {
      "x": 337.76684060721067,
      "y": 438.21457575991957,
    },
    "battery-direction-arrow": {
      "x": 392.7404530360531,
      "y": 449.7377020651249,
    },
    "ev-label": {
      "x": 225.27128795066412,
      "y": 269.4726003557893,
    },
    "ev-power": {
      "x": 224.41072699240988,
      "y": 291.1646299017712,
    },
    "ev-pct": {
      "x": 225.0,
      "y": 327.0,
    },
    "ev2-label": {
      "x": 102.2266366223909,
      "y": 226.49769897130483,
    },
    "ev2-power": {
      "x": 100.47883064516128,
      "y": 248.060754892103,
    },
    "ev2-pct": {
      "x": 133.81389350094878,
      "y": 296.33469332508315,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 479.50367647058823,
      "y1": 393.124951659061,
      "x2": 482.0920303605313,
      "y2": 341.8757251140846,
    },
    "load-guide": {
      "x1": 406.0,
      "y1": 242.0,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 316.2016722011385,
      "y1": 429.535540258334,
      "x2": 317.88276802656543,
      "y2": 374.02573671591,
    },
  },
  "scene_day_cloudy_idle.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 425.17715251423147,
      "y": 442.7264289581561,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 279.84686314041744,
      "y": 445.31924356098693,
    },
    "battery-direction-arrow": {
      "x": 338.30496916508537,
      "y": 456.46212004021965,
    },
    "ev-label": {
      "x": 162.0,
      "y": 177.0,
    },
    "ev-power": {
      "x": 178.0,
      "y": 205.0,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.94823292220116,
      "y1": 390.9034921494315,
      "x2": 432.0505218216319,
      "y2": 342.5784090030165,
    },
    "load-guide": {
      "x1": 405.88902395635677,
      "y1": 248.8145834944698,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_day_rain_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 423.85851518026567,
      "y": 440.27593007966584,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 280.48283325426945,
      "y": 445.5638487121974,
    },
    "battery-direction-arrow": {
      "x": 339.1744248102467,
      "y": 455.8683966277361,
    },
    "ev-label": {
      "x": 170.05529530360533,
      "y": 258.72776316807176,
    },
    "ev-power": {
      "x": 169.44156190702088,
      "y": 280.4509242787532,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.9371145635673,
      "y1": 389.16234820945164,
      "x2": 431.9749169829222,
      "y2": 339.51862092969293,
    },
    "load-guide": {
      "x1": 405.91348434535104,
      "y1": 250.31334596643208,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_day_rain_dual_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 327.06653225806457,
      "y": 80.40171320287725,
    },
    "grid-label": {
      "x": 492.1541449240987,
      "y": 398.98213318895506,
    },
    "grid-power": {
      "x": 491.424780597723,
      "y": 418.8529662000155,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 336.328125,
      "y": 407.46326088637943,
    },
    "battery-label": {
      "x": 336.71726755218214,
      "y": 423.4537667259649,
    },
    "battery-power": {
      "x": 337.76684060721067,
      "y": 438.21457575991957,
    },
    "battery-direction-arrow": {
      "x": 392.7404530360531,
      "y": 449.7377020651249,
    },
    "ev-label": {
      "x": 225.27128795066412,
      "y": 269.4726003557893,
    },
    "ev-power": {
      "x": 224.41072699240988,
      "y": 291.1646299017712,
    },
    "ev-pct": {
      "x": 225.0,
      "y": 327.0,
    },
    "ev2-label": {
      "x": 102.2266366223909,
      "y": 226.49769897130483,
    },
    "ev2-power": {
      "x": 100.47883064516128,
      "y": 248.060754892103,
    },
    "ev2-pct": {
      "x": 133.81389350094878,
      "y": 296.33469332508315,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 479.50367647058823,
      "y1": 393.124951659061,
      "x2": 482.0920303605313,
      "y2": 341.8757251140846,
    },
    "load-guide": {
      "x1": 406.0,
      "y1": 242.0,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 316.2016722011385,
      "y1": 429.535540258334,
      "x2": 317.88276802656543,
      "y2": 374.02573671591,
    },
  },
  "scene_day_rain_idle.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 425.17715251423147,
      "y": 442.7264289581561,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 279.84686314041744,
      "y": 445.31924356098693,
    },
    "battery-direction-arrow": {
      "x": 338.30496916508537,
      "y": 456.46212004021965,
    },
    "ev-label": {
      "x": 162.0,
      "y": 177.0,
    },
    "ev-power": {
      "x": 178.0,
      "y": 205.0,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.94823292220116,
      "y1": 390.9034921494315,
      "x2": 432.0505218216319,
      "y2": 342.5784090030165,
    },
    "load-guide": {
      "x1": 405.88902395635677,
      "y1": 248.8145834944698,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_day_snow_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 423.85851518026567,
      "y": 440.27593007966584,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 280.48283325426945,
      "y": 445.5638487121974,
    },
    "battery-direction-arrow": {
      "x": 339.1744248102467,
      "y": 455.8683966277361,
    },
    "ev-label": {
      "x": 170.05529530360533,
      "y": 258.72776316807176,
    },
    "ev-power": {
      "x": 169.44156190702088,
      "y": 280.4509242787532,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.9371145635673,
      "y1": 389.16234820945164,
      "x2": 431.9749169829222,
      "y2": 339.51862092969293,
    },
    "load-guide": {
      "x1": 405.91348434535104,
      "y1": 250.31334596643208,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_day_snow_dual_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 327.06653225806457,
      "y": 80.40171320287725,
    },
    "grid-label": {
      "x": 492.1541449240987,
      "y": 398.98213318895506,
    },
    "grid-power": {
      "x": 491.424780597723,
      "y": 418.8529662000155,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 336.328125,
      "y": 407.46326088637943,
    },
    "battery-label": {
      "x": 336.71726755218214,
      "y": 423.4537667259649,
    },
    "battery-power": {
      "x": 337.76684060721067,
      "y": 438.21457575991957,
    },
    "battery-direction-arrow": {
      "x": 392.7404530360531,
      "y": 449.7377020651249,
    },
    "ev-label": {
      "x": 225.27128795066412,
      "y": 269.4726003557893,
    },
    "ev-power": {
      "x": 224.41072699240988,
      "y": 291.1646299017712,
    },
    "ev-pct": {
      "x": 225.0,
      "y": 327.0,
    },
    "ev2-label": {
      "x": 102.2266366223909,
      "y": 226.49769897130483,
    },
    "ev2-power": {
      "x": 100.47883064516128,
      "y": 248.060754892103,
    },
    "ev2-pct": {
      "x": 133.81389350094878,
      "y": 296.33469332508315,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 479.50367647058823,
      "y1": 393.124951659061,
      "x2": 482.0920303605313,
      "y2": 341.8757251140846,
    },
    "load-guide": {
      "x1": 406.0,
      "y1": 242.0,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 316.2016722011385,
      "y1": 429.535540258334,
      "x2": 317.88276802656543,
      "y2": 374.02573671591,
    },
  },
  "scene_day_snow_idle.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 425.17715251423147,
      "y": 442.7264289581561,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 279.84686314041744,
      "y": 445.31924356098693,
    },
    "battery-direction-arrow": {
      "x": 338.30496916508537,
      "y": 456.46212004021965,
    },
    "ev-label": {
      "x": 162.0,
      "y": 177.0,
    },
    "ev-power": {
      "x": 178.0,
      "y": 205.0,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.94823292220116,
      "y1": 390.9034921494315,
      "x2": 432.0505218216319,
      "y2": 342.5784090030165,
    },
    "load-guide": {
      "x1": 405.88902395635677,
      "y1": 248.8145834944698,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_day_storm_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 423.85851518026567,
      "y": 440.27593007966584,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 280.48283325426945,
      "y": 445.5638487121974,
    },
    "battery-direction-arrow": {
      "x": 339.1744248102467,
      "y": 455.8683966277361,
    },
    "ev-label": {
      "x": 170.05529530360533,
      "y": 258.72776316807176,
    },
    "ev-power": {
      "x": 169.44156190702088,
      "y": 280.4509242787532,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.9371145635673,
      "y1": 389.16234820945164,
      "x2": 431.9749169829222,
      "y2": 339.51862092969293,
    },
    "load-guide": {
      "x1": 405.91348434535104,
      "y1": 250.31334596643208,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_day_storm_dual_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 327.06653225806457,
      "y": 80.40171320287725,
    },
    "grid-label": {
      "x": 492.1541449240987,
      "y": 398.98213318895506,
    },
    "grid-power": {
      "x": 491.424780597723,
      "y": 418.8529662000155,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 336.328125,
      "y": 407.46326088637943,
    },
    "battery-label": {
      "x": 336.71726755218214,
      "y": 423.4537667259649,
    },
    "battery-power": {
      "x": 337.76684060721067,
      "y": 438.21457575991957,
    },
    "battery-direction-arrow": {
      "x": 392.7404530360531,
      "y": 449.7377020651249,
    },
    "ev-label": {
      "x": 225.27128795066412,
      "y": 269.4726003557893,
    },
    "ev-power": {
      "x": 224.41072699240988,
      "y": 291.1646299017712,
    },
    "ev-pct": {
      "x": 225.0,
      "y": 327.0,
    },
    "ev2-label": {
      "x": 102.2266366223909,
      "y": 226.49769897130483,
    },
    "ev2-power": {
      "x": 100.47883064516128,
      "y": 248.060754892103,
    },
    "ev2-pct": {
      "x": 133.81389350094878,
      "y": 296.33469332508315,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 479.50367647058823,
      "y1": 393.124951659061,
      "x2": 482.0920303605313,
      "y2": 341.8757251140846,
    },
    "load-guide": {
      "x1": 406.0,
      "y1": 242.0,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 316.2016722011385,
      "y1": 429.535540258334,
      "x2": 317.88276802656543,
      "y2": 374.02573671591,
    },
  },
  "scene_day_storm_idle.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 425.17715251423147,
      "y": 442.7264289581561,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 279.84686314041744,
      "y": 445.31924356098693,
    },
    "battery-direction-arrow": {
      "x": 338.30496916508537,
      "y": 456.46212004021965,
    },
    "ev-label": {
      "x": 162.0,
      "y": 177.0,
    },
    "ev-power": {
      "x": 178.0,
      "y": 205.0,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.94823292220116,
      "y1": 390.9034921494315,
      "x2": 432.0505218216319,
      "y2": 342.5784090030165,
    },
    "load-guide": {
      "x1": 405.88902395635677,
      "y1": 248.8145834944698,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_night_clear_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 423.85851518026567,
      "y": 440.27593007966584,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 280.48283325426945,
      "y": 445.5638487121974,
    },
    "battery-direction-arrow": {
      "x": 339.1744248102467,
      "y": 455.8683966277361,
    },
    "ev-label": {
      "x": 170.05529530360533,
      "y": 258.72776316807176,
    },
    "ev-power": {
      "x": 169.44156190702088,
      "y": 280.4509242787532,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.9371145635673,
      "y1": 389.16234820945164,
      "x2": 431.9749169829222,
      "y2": 339.51862092969293,
    },
    "load-guide": {
      "x1": 405.91348434535104,
      "y1": 250.31334596643208,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_night_clear_dual_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 327.06653225806457,
      "y": 80.40171320287725,
    },
    "grid-label": {
      "x": 492.1541449240987,
      "y": 398.98213318895506,
    },
    "grid-power": {
      "x": 491.424780597723,
      "y": 418.8529662000155,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 336.328125,
      "y": 407.46326088637943,
    },
    "battery-label": {
      "x": 336.71726755218214,
      "y": 423.4537667259649,
    },
    "battery-power": {
      "x": 337.76684060721067,
      "y": 438.21457575991957,
    },
    "battery-direction-arrow": {
      "x": 392.7404530360531,
      "y": 449.7377020651249,
    },
    "ev-label": {
      "x": 225.27128795066412,
      "y": 269.4726003557893,
    },
    "ev-power": {
      "x": 224.41072699240988,
      "y": 291.1646299017712,
    },
    "ev-pct": {
      "x": 225.0,
      "y": 327.0,
    },
    "ev2-label": {
      "x": 102.2266366223909,
      "y": 226.49769897130483,
    },
    "ev2-power": {
      "x": 100.47883064516128,
      "y": 248.060754892103,
    },
    "ev2-pct": {
      "x": 133.81389350094878,
      "y": 296.33469332508315,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 479.50367647058823,
      "y1": 393.124951659061,
      "x2": 482.0920303605313,
      "y2": 341.8757251140846,
    },
    "load-guide": {
      "x1": 406.0,
      "y1": 242.0,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 316.2016722011385,
      "y1": 429.535540258334,
      "x2": 317.88276802656543,
      "y2": 374.02573671591,
    },
  },
  "scene_night_clear_idle.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 425.17715251423147,
      "y": 442.7264289581561,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 279.84686314041744,
      "y": 445.31924356098693,
    },
    "battery-direction-arrow": {
      "x": 338.30496916508537,
      "y": 456.46212004021965,
    },
    "ev-label": {
      "x": 162.0,
      "y": 177.0,
    },
    "ev-power": {
      "x": 178.0,
      "y": 205.0,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.94823292220116,
      "y1": 390.9034921494315,
      "x2": 432.0505218216319,
      "y2": 342.5784090030165,
    },
    "load-guide": {
      "x1": 405.88902395635677,
      "y1": 248.8145834944698,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_night_cloudy_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 423.85851518026567,
      "y": 440.27593007966584,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 280.48283325426945,
      "y": 445.5638487121974,
    },
    "battery-direction-arrow": {
      "x": 339.1744248102467,
      "y": 455.8683966277361,
    },
    "ev-label": {
      "x": 170.05529530360533,
      "y": 258.72776316807176,
    },
    "ev-power": {
      "x": 169.44156190702088,
      "y": 280.4509242787532,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.9371145635673,
      "y1": 389.16234820945164,
      "x2": 431.9749169829222,
      "y2": 339.51862092969293,
    },
    "load-guide": {
      "x1": 405.91348434535104,
      "y1": 250.31334596643208,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_night_cloudy_dual_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 327.06653225806457,
      "y": 80.40171320287725,
    },
    "grid-label": {
      "x": 492.1541449240987,
      "y": 398.98213318895506,
    },
    "grid-power": {
      "x": 491.424780597723,
      "y": 418.8529662000155,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 336.328125,
      "y": 407.46326088637943,
    },
    "battery-label": {
      "x": 336.71726755218214,
      "y": 423.4537667259649,
    },
    "battery-power": {
      "x": 337.76684060721067,
      "y": 438.21457575991957,
    },
    "battery-direction-arrow": {
      "x": 392.7404530360531,
      "y": 449.7377020651249,
    },
    "ev-label": {
      "x": 225.27128795066412,
      "y": 269.4726003557893,
    },
    "ev-power": {
      "x": 224.41072699240988,
      "y": 291.1646299017712,
    },
    "ev-pct": {
      "x": 225.0,
      "y": 327.0,
    },
    "ev2-label": {
      "x": 102.2266366223909,
      "y": 226.49769897130483,
    },
    "ev2-power": {
      "x": 100.47883064516128,
      "y": 248.060754892103,
    },
    "ev2-pct": {
      "x": 133.81389350094878,
      "y": 296.33469332508315,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 479.50367647058823,
      "y1": 393.124951659061,
      "x2": 482.0920303605313,
      "y2": 341.8757251140846,
    },
    "load-guide": {
      "x1": 406.0,
      "y1": 242.0,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 316.2016722011385,
      "y1": 429.535540258334,
      "x2": 317.88276802656543,
      "y2": 374.02573671591,
    },
  },
  "scene_night_cloudy_idle.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 425.17715251423147,
      "y": 442.7264289581561,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 279.84686314041744,
      "y": 445.31924356098693,
    },
    "battery-direction-arrow": {
      "x": 338.30496916508537,
      "y": 456.46212004021965,
    },
    "ev-label": {
      "x": 162.0,
      "y": 177.0,
    },
    "ev-power": {
      "x": 178.0,
      "y": 205.0,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.94823292220116,
      "y1": 390.9034921494315,
      "x2": 432.0505218216319,
      "y2": 342.5784090030165,
    },
    "load-guide": {
      "x1": 405.88902395635677,
      "y1": 248.8145834944698,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_night_rain_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 423.85851518026567,
      "y": 440.27593007966584,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 280.48283325426945,
      "y": 445.5638487121974,
    },
    "battery-direction-arrow": {
      "x": 339.1744248102467,
      "y": 455.8683966277361,
    },
    "ev-label": {
      "x": 170.05529530360533,
      "y": 258.72776316807176,
    },
    "ev-power": {
      "x": 169.44156190702088,
      "y": 280.4509242787532,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.9371145635673,
      "y1": 389.16234820945164,
      "x2": 431.9749169829222,
      "y2": 339.51862092969293,
    },
    "load-guide": {
      "x1": 405.91348434535104,
      "y1": 250.31334596643208,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_night_rain_dual_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 327.06653225806457,
      "y": 80.40171320287725,
    },
    "grid-label": {
      "x": 492.1541449240987,
      "y": 398.98213318895506,
    },
    "grid-power": {
      "x": 491.424780597723,
      "y": 418.8529662000155,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 336.328125,
      "y": 407.46326088637943,
    },
    "battery-label": {
      "x": 336.71726755218214,
      "y": 423.4537667259649,
    },
    "battery-power": {
      "x": 337.76684060721067,
      "y": 438.21457575991957,
    },
    "battery-direction-arrow": {
      "x": 392.7404530360531,
      "y": 449.7377020651249,
    },
    "ev-label": {
      "x": 225.27128795066412,
      "y": 269.4726003557893,
    },
    "ev-power": {
      "x": 224.41072699240988,
      "y": 291.1646299017712,
    },
    "ev-pct": {
      "x": 225.0,
      "y": 327.0,
    },
    "ev2-label": {
      "x": 102.2266366223909,
      "y": 226.49769897130483,
    },
    "ev2-power": {
      "x": 100.47883064516128,
      "y": 248.060754892103,
    },
    "ev2-pct": {
      "x": 133.81389350094878,
      "y": 296.33469332508315,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 479.50367647058823,
      "y1": 393.124951659061,
      "x2": 482.0920303605313,
      "y2": 341.8757251140846,
    },
    "load-guide": {
      "x1": 406.0,
      "y1": 242.0,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 316.2016722011385,
      "y1": 429.535540258334,
      "x2": 317.88276802656543,
      "y2": 374.02573671591,
    },
  },
  "scene_night_rain_idle.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 425.17715251423147,
      "y": 442.7264289581561,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 279.84686314041744,
      "y": 445.31924356098693,
    },
    "battery-direction-arrow": {
      "x": 338.30496916508537,
      "y": 456.46212004021965,
    },
    "ev-label": {
      "x": 162.0,
      "y": 177.0,
    },
    "ev-power": {
      "x": 178.0,
      "y": 205.0,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.94823292220116,
      "y1": 390.9034921494315,
      "x2": 432.0505218216319,
      "y2": 342.5784090030165,
    },
    "load-guide": {
      "x1": 405.88902395635677,
      "y1": 248.8145834944698,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_night_snow_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 423.85851518026567,
      "y": 440.27593007966584,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 280.48283325426945,
      "y": 445.5638487121974,
    },
    "battery-direction-arrow": {
      "x": 339.1744248102467,
      "y": 455.8683966277361,
    },
    "ev-label": {
      "x": 170.05529530360533,
      "y": 258.72776316807176,
    },
    "ev-power": {
      "x": 169.44156190702088,
      "y": 280.4509242787532,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.9371145635673,
      "y1": 389.16234820945164,
      "x2": 431.9749169829222,
      "y2": 339.51862092969293,
    },
    "load-guide": {
      "x1": 405.91348434535104,
      "y1": 250.31334596643208,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_night_snow_dual_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 327.06653225806457,
      "y": 80.40171320287725,
    },
    "grid-label": {
      "x": 492.1541449240987,
      "y": 398.98213318895506,
    },
    "grid-power": {
      "x": 491.424780597723,
      "y": 418.8529662000155,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 336.328125,
      "y": 407.46326088637943,
    },
    "battery-label": {
      "x": 336.71726755218214,
      "y": 423.4537667259649,
    },
    "battery-power": {
      "x": 337.76684060721067,
      "y": 438.21457575991957,
    },
    "battery-direction-arrow": {
      "x": 392.7404530360531,
      "y": 449.7377020651249,
    },
    "ev-label": {
      "x": 225.27128795066412,
      "y": 269.4726003557893,
    },
    "ev-power": {
      "x": 224.41072699240988,
      "y": 291.1646299017712,
    },
    "ev-pct": {
      "x": 225.0,
      "y": 327.0,
    },
    "ev2-label": {
      "x": 102.2266366223909,
      "y": 226.49769897130483,
    },
    "ev2-power": {
      "x": 100.47883064516128,
      "y": 248.060754892103,
    },
    "ev2-pct": {
      "x": 133.81389350094878,
      "y": 296.33469332508315,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 479.50367647058823,
      "y1": 393.124951659061,
      "x2": 482.0920303605313,
      "y2": 341.8757251140846,
    },
    "load-guide": {
      "x1": 406.0,
      "y1": 242.0,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 316.2016722011385,
      "y1": 429.535540258334,
      "x2": 317.88276802656543,
      "y2": 374.02573671591,
    },
  },
  "scene_night_snow_idle.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 425.17715251423147,
      "y": 442.7264289581561,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 279.84686314041744,
      "y": 445.31924356098693,
    },
    "battery-direction-arrow": {
      "x": 338.30496916508537,
      "y": 456.46212004021965,
    },
    "ev-label": {
      "x": 162.0,
      "y": 177.0,
    },
    "ev-power": {
      "x": 178.0,
      "y": 205.0,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.94823292220116,
      "y1": 390.9034921494315,
      "x2": 432.0505218216319,
      "y2": 342.5784090030165,
    },
    "load-guide": {
      "x1": 405.88902395635677,
      "y1": 248.8145834944698,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_night_storm_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 423.85851518026567,
      "y": 440.27593007966584,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 280.48283325426945,
      "y": 445.5638487121974,
    },
    "battery-direction-arrow": {
      "x": 339.1744248102467,
      "y": 455.8683966277361,
    },
    "ev-label": {
      "x": 170.05529530360533,
      "y": 258.72776316807176,
    },
    "ev-power": {
      "x": 169.44156190702088,
      "y": 280.4509242787532,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.9371145635673,
      "y1": 389.16234820945164,
      "x2": 431.9749169829222,
      "y2": 339.51862092969293,
    },
    "load-guide": {
      "x1": 405.91348434535104,
      "y1": 250.31334596643208,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
  "scene_night_storm_dual_charging.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 327.06653225806457,
      "y": 80.40171320287725,
    },
    "grid-label": {
      "x": 492.1541449240987,
      "y": 398.98213318895506,
    },
    "grid-power": {
      "x": 491.424780597723,
      "y": 418.8529662000155,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 336.328125,
      "y": 407.46326088637943,
    },
    "battery-label": {
      "x": 336.71726755218214,
      "y": 423.4537667259649,
    },
    "battery-power": {
      "x": 337.76684060721067,
      "y": 438.21457575991957,
    },
    "battery-direction-arrow": {
      "x": 392.7404530360531,
      "y": 449.7377020651249,
    },
    "ev-label": {
      "x": 225.27128795066412,
      "y": 269.4726003557893,
    },
    "ev-power": {
      "x": 224.41072699240988,
      "y": 291.1646299017712,
    },
    "ev-pct": {
      "x": 225.0,
      "y": 327.0,
    },
    "ev2-label": {
      "x": 102.2266366223909,
      "y": 226.49769897130483,
    },
    "ev2-power": {
      "x": 100.47883064516128,
      "y": 248.060754892103,
    },
    "ev2-pct": {
      "x": 133.81389350094878,
      "y": 296.33469332508315,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 479.50367647058823,
      "y1": 393.124951659061,
      "x2": 482.0920303605313,
      "y2": 341.8757251140846,
    },
    "load-guide": {
      "x1": 406.0,
      "y1": 242.0,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 316.2016722011385,
      "y1": 429.535540258334,
      "x2": 317.88276802656543,
      "y2": 374.02573671591,
    },
  },
  "scene_night_storm_idle.png": {
    "solar-label": {
      "x": 329.0,
      "y": 60.0,
    },
    "solar-power": {
      "x": 329.0,
      "y": 84.0,
    },
    "grid-label": {
      "x": 425.0,
      "y": 420.0,
    },
    "grid-power": {
      "x": 425.17715251423147,
      "y": 442.7264289581561,
    },
    "load-label": {
      "x": 394.0,
      "y": 216.0,
    },
    "load-power": {
      "x": 394.0,
      "y": 238.0,
    },
    "battery-pct": {
      "x": 279.0,
      "y": 412.0,
    },
    "battery-label": {
      "x": 279.0,
      "y": 430.0,
    },
    "battery-power": {
      "x": 279.84686314041744,
      "y": 445.31924356098693,
    },
    "battery-direction-arrow": {
      "x": 338.30496916508537,
      "y": 456.46212004021965,
    },
    "ev-label": {
      "x": 162.0,
      "y": 177.0,
    },
    "ev-power": {
      "x": 178.0,
      "y": 205.0,
    },
    "ev-pct": {
      "x": 176.0,
      "y": 321.0,
    },
    "solar-guide": {
      "x1": 320.0,
      "y1": 94.0,
      "x2": 320.0,
      "y2": 166.0,
    },
    "grid-guide": {
      "x1": 431.94823292220116,
      "y1": 390.9034921494315,
      "x2": 432.0505218216319,
      "y2": 342.5784090030165,
    },
    "load-guide": {
      "x1": 405.88902395635677,
      "y1": 248.8145834944698,
      "x2": 406.0,
      "y2": 314.0,
    },
    "battery-guide": {
      "x1": 270.0,
      "y1": 446.0,
      "x2": 270.0,
      "y2": 388.0,
    },
  },
});
